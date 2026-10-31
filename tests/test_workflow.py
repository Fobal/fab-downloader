# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fabdl import download
from fabdl.auth import Session
from fabdl.download import DownloadCancelled, DownloadError
from fabdl.fab import DownloadSource, LibraryItem, ProjectVersion
from fabdl.install import RECORD_FILE, InstallError, InstallRecord, installed_plugins
from fabdl.manifest import ChunkInfo, chunk_path
from fabdl.project import load_project, read_engine
from fabdl.workflow import (
    UpdateStatus,
    check_updates,
    download_item,
    fetch_manifest,
    add_to_project,
    install_to_engine,
    result_lines,
    output_folder,
    folders_with_plugins,
    safe_folder_name,
    unreal_library,
)

from . import builders
from .builders import GUID_A, GUID_B
from .fakes import FakeHttp, RoutedHttp, make_project

SESSION = Session("token", datetime(2030, 1, 1, tzinfo=timezone.utc), "refresh", "account1", "Me")
ITEM = LibraryItem("asset1", "ns1", "Flow: Graph", "CODE_PLUGIN", (ProjectVersion("art53", ("UE_5.3",)),))
CHUNK_URL = f"https://up/{chunk_path(ChunkInfo(GUID_A, 5, 1), 18)}"
CHUNK_B_URL = f"https://up/{chunk_path(ChunkInfo(GUID_B, 6, 1), 18)}"


def _manifest_bytes(files, chunks=((GUID_A, 5, 1),)):
    body = (
        builders.meta(18, "2.0")
        + builders.chunk_list(list(chunks), 18)
        + builders.file_list(files)
    )
    return builders.binary_manifest(body)


def _two_chunk_cdn():
    """Like _cdn, for a plugin whose descriptor is in one chunk and its source in another."""
    http = _cdn([
        ("FlowGraph/FlowGraph.uplugin", hashlib.sha1(b"{}").digest(), [(GUID_A, 0, 2)]),
        ("FlowGraph/Source/A.cpp", hashlib.sha1(b"code").digest(), [(GUID_B, 0, 4)]),
    ], chunks=((GUID_A, 5, 1), (GUID_B, 6, 1)))
    http.routes[f"GET {CHUNK_URL}"] = builders.chunk_file(GUID_A, b"{}")
    http.routes[f"GET {CHUNK_B_URL}"] = builders.chunk_file(GUID_B, b"code")
    return http


def _cdn(files, chunks=((GUID_A, 5, 1),)):
    """Fab and CDN answers for a build of ITEM. The first CDN is down."""
    return RoutedHttp({
        "POST https://www.fab.com/e/artifacts/art53/manifest": {"downloadInfo": [{
            "artifactId": "art53",
            "buildVersion": "1.4.0-5.3",
            "distributionPointBaseUrls": ["https://down/", "https://up/"],
            "distributionPoints": [
                {"manifestUrl": "https://down/m.manifest?sig"},
                {"manifestUrl": "https://up/m.manifest?sig"},
            ],
        }]},
        "GET https://up/m.manifest?sig": _manifest_bytes(files, chunks),
        f"GET {CHUNK_URL}": builders.chunk_file(GUID_A, b"{}code"),
    })


PLUGIN_FILES = [
    ("FlowGraph/FlowGraph.uplugin", hashlib.sha1(b"{}").digest(), [(GUID_A, 0, 2)]),
    ("FlowGraph/Source/A.cpp", hashlib.sha1(b"code").digest(), [(GUID_A, 2, 4)]),
]


class DownloadAndInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_downloads_from_the_cdn_that_serves_the_manifest(self):
        http = _cdn(PLUGIN_FILES)
        out = self.root / "out"
        announced = []

        paths = download_item(
            http, SESSION, ITEM, ITEM.project_versions[0], out, on_manifest=lambda m: announced.append(m.build_version)
        )

        self.assertEqual((out / "FlowGraph" / "Source" / "A.cpp").read_bytes(), b"code")
        self.assertEqual(folders_with_plugins(paths), [out / "FlowGraph"])
        self.assertEqual(announced, ["2.0"])
        self.assertNotIn(f"GET {CHUNK_URL.replace('https://up/', 'https://down/')}", http.requests)

    def test_adds_a_plugin_to_the_project_and_records_the_build(self):
        project = load_project(make_project(self.root / "engine"))
        progress = []

        result = add_to_project(
            _cdn(PLUGIN_FILES), SESSION, ITEM, ITEM.project_versions[0], "UE_5.3", project,
            report=lambda done, total: progress.append((done, total)),
        )

        folder = project.plugins_dir / "FlowGraph"
        self.assertEqual(result.plugin.folder, folder)
        self.assertEqual((folder / "Source" / "A.cpp").read_bytes(), b"code")
        record = InstallRecord.from_json((folder / RECORD_FILE).read_text())
        self.assertEqual(
            (record.asset_id, record.namespace, record.artifact_id, record.engine_version, record.build_version),
            ("asset1", "ns1", "art53", "UE_5.3", "1.4.0-5.3"),
        )
        self.assertEqual([plugin.record for plugin in installed_plugins(project.plugins_dir)], [record])
        self.assertEqual(progress, [(1, 1)])
        self.assertFalse((project.root / "Saved" / "fabdl").exists(), "the download folder is removed")
        self.assertEqual(
            result_lines(result),
            [f"Installed Flow: Graph (UE_5.3) into {folder}", "Build the project to compile the plugin."],
        )

    def test_adds_an_asset_pack_to_the_projects_content(self):
        project = load_project(make_project(self.root / "engine"))
        http = _cdn([("Content/RockPack/Rock.uasset", hashlib.sha1(b"{}").digest(), [(GUID_A, 0, 2)])])

        result = add_to_project(http, SESSION, ITEM, ITEM.project_versions[0], "UE_5.3", project)

        rock = project.root / "Content" / "RockPack" / "Rock.uasset"
        self.assertEqual(rock.read_bytes(), b"{}")
        self.assertFalse(project.plugins_dir.exists())
        self.assertFalse((project.root / "Saved" / "fabdl").exists())
        self.assertEqual(result_lines(result), [f"Added 1 file to {project.root / 'Content'} (RockPack)"])

    def test_installs_a_plugin_into_an_engine(self):
        engine_root = self.root / "engine"
        make_project(engine_root)
        engine = read_engine(engine_root)

        result = install_to_engine(_cdn(PLUGIN_FILES), SESSION, ITEM, ITEM.project_versions[0], engine)

        folder = engine_root / "Engine" / "Plugins" / "Marketplace" / "FlowGraph"
        self.assertEqual(result.plugin.folder, folder)
        self.assertFalse((engine_root / "Engine" / "Intermediate" / "fabdl").exists())
        self.assertIn("The plugin is off by default. Enable it in each project's Edit > Plugins.", result_lines(result))

    def test_engines_take_only_plugins(self):
        engine_root = self.root / "engine"
        make_project(engine_root)
        http = _cdn([("Content/Rock.uasset", hashlib.sha1(b"{}").digest(), [(GUID_A, 0, 2)])])

        with self.assertRaisesRegex(InstallError, "not a code plugin"):
            install_to_engine(http, SESSION, ITEM, ITEM.project_versions[0], read_engine(engine_root))

        self.assertFalse((engine_root / "Engine" / "Plugins").exists())

    def test_a_failed_install_is_resumed_by_the_next_one(self):
        project = load_project(make_project(self.root / "engine"))
        http = _two_chunk_cdn()
        chunk_b = http.routes.pop(f"GET {CHUNK_B_URL}")

        with mock.patch.object(download, "RETRY_DELAYS", ()), self.assertRaises(DownloadError):
            add_to_project(http, SESSION, ITEM, ITEM.project_versions[0], "UE_5.3", project)

        self.assertFalse(project.plugins_dir.exists())
        http.routes[f"GET {CHUNK_B_URL}"] = chunk_b
        http.requests.clear()

        add_to_project(http, SESSION, ITEM, ITEM.project_versions[0], "UE_5.3", project)

        self.assertNotIn(f"GET {CHUNK_URL}", http.requests, "the chunk from the first attempt is reused")
        self.assertIn(f"GET {CHUNK_B_URL}", http.requests)
        self.assertEqual((project.plugins_dir / "FlowGraph" / "Source" / "A.cpp").read_bytes(), b"code")
        self.assertFalse((project.root / "Saved" / "fabdl").exists())

    def test_cancelling_installs_nothing_and_cleans_up(self):
        project = load_project(make_project(self.root / "engine"))
        stop = threading.Event()

        with self.assertRaises(DownloadCancelled):
            add_to_project(
                _cdn(PLUGIN_FILES), SESSION, ITEM, ITEM.project_versions[0], "UE_5.3", project,
                report=lambda done, total: stop.set(), stop=stop,
            )

        self.assertFalse(project.plugins_dir.exists())
        self.assertFalse((project.root / "Saved" / "fabdl").exists())


class CheckUpdatesTests(unittest.TestCase):
    def _record(self, asset_id="asset1", artifact_id="art53", build_version="1.0", engine_version="UE_5.3"):
        return InstallRecord(asset_id, "ns1", artifact_id, "Flow", engine_version, build_version, "Windows", "")

    def _answer(self, build_version):
        return (200, {"downloadInfo": [{
            "artifactId": "art53",
            "buildVersion": build_version,
            "distributionPointBaseUrls": ["https://cdn"],
            "distributionPoints": [{"manifestUrl": "https://cdn/m"}],
        }]})

    def test_compares_the_installed_build_with_fabs(self):
        http = FakeHttp(self._answer("1.0"), self._answer("1.1"))

        statuses = check_updates(http, SESSION, [self._record(), self._record(build_version="0.9")], [ITEM])

        self.assertEqual([s.describe() for s in statuses], ["up to date", "update available"])
        self.assertEqual(statuses[1].latest_build_version, "1.1")

    def test_a_new_artifact_is_an_update(self):
        status = check_updates(FakeHttp(self._answer("1.0")), SESSION, [self._record(artifact_id="old")], [ITEM])[0]

        self.assertTrue(status.update_available)

    def test_items_gone_from_the_library_need_no_request(self):
        http = FakeHttp()
        records = [self._record(asset_id="gone"), self._record(engine_version="UE_5.1")]

        statuses = check_updates(http, SESSION, records, [ITEM])

        self.assertEqual(http.requests, [])
        self.assertFalse(any(s.update_available for s in statuses))
        self.assertEqual(statuses[1].describe(), "no longer in your library for UE_5.1")

    def test_status_wording(self):
        self.assertEqual(UpdateStatus(self._record(), "art53", "1.0").describe(), "up to date")
        self.assertEqual(UpdateStatus(self._record(), "art53", "2.0").describe(), "update available")


class FetchManifestTests(unittest.TestCase):
    sources = [DownloadSource("https://a/m?sig", "https://a"), DownloadSource("https://b/m?sig", "https://b")]

    def test_moves_the_working_cdn_first(self):
        http = FakeHttp((503, b""), (200, _manifest_bytes([])))

        manifest, ordered = fetch_manifest(http, self.sources)

        self.assertEqual(manifest.build_version, "2.0")
        self.assertEqual([s.base_url for s in ordered], ["https://b", "https://a"])

    def test_fails_when_no_cdn_serves_a_manifest(self):
        http = FakeHttp((503, b""), (200, b"garbage"))

        with self.assertRaisesRegex(DownloadError, "Could not download the build manifest"):
            fetch_manifest(http, self.sources)


class HelperTests(unittest.TestCase):
    def test_unreal_library_hides_items_without_engine_builds(self):
        page = {"cursors": {}, "results": [
            {"assetId": "a1", "assetNamespace": "n", "title": "Plugin", "projectVersions": [{"artifactId": "x", "engineVersions": ["UE_5.3"]}]},
            {"assetId": "a2", "assetNamespace": "n", "title": "Model", "projectVersions": []},
        ]}

        self.assertEqual([i.asset_id for i in unreal_library(FakeHttp((200, page)), SESSION)], ["a1"])

    def test_output_folder(self):
        self.assertEqual(output_folder(Path("G:/Downloads"), ITEM, "UE_5.3"), Path("G:/Downloads/Flow_ Graph UE_5.3"))

    def test_safe_folder_name(self):
        self.assertEqual(safe_folder_name('Flow: "Graph" UE_5.3'), "Flow_ _Graph_ UE_5.3")
        self.assertEqual(safe_folder_name("a/b\\c?. "), "a_b_c_")
        self.assertEqual(safe_folder_name(" . "), "fab-download")

    def test_folders_with_plugins(self):
        paths = [Path("o/A/A.uplugin"), Path("o/A/Source/x.cpp"), Path("o/B/B.UPLUGIN")]

        self.assertEqual(folders_with_plugins(paths), [Path("o/A"), Path("o/B")])


if __name__ == "__main__":
    unittest.main()
