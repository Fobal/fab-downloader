# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from fabdl.auth import Session
from fabdl.fab import (
    DownloadSource,
    FabError,
    ProjectVersion,
    engine_sort_key,
    find_items,
    list_library,
    normalize_engine_version,
    pair_sources,
    parse_library_item,
    request_build,
)
from fabdl.net import HttpError

from .fakes import FakeHttp

SESSION = Session("token", datetime(2030, 1, 1, tzinfo=timezone.utc), "refresh", "account1", "Me")


def _raw_item(asset_id="asset1", title="Flow Graph", versions=(("art53", ["UE_5.3"]), ("art52", ["UE_5.2", "UE_5.1"]))):
    return {
        "assetId": asset_id,
        "assetNamespace": "ns1",
        "title": title,
        "distributionMethod": "CODE_PLUGIN",
        "projectVersions": [
            {"artifactId": artifact, "engineVersions": engines, "targetPlatforms": ["Windows"], "buildVersions": []}
            for artifact, engines in versions
        ],
    }


class EngineVersionTests(unittest.TestCase):
    def test_normalizes_common_spellings(self):
        for text in ["5.3", "UE_5.3", "ue5.3", " UE5.3 ", "5.03"]:
            with self.subTest(text=text):
                self.assertEqual(normalize_engine_version(text), "UE_5.3")

    def test_rejects_other_text(self):
        for text in ["5", "5.3.1", "latest", "UE-5.3"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_engine_version(text)

    def test_sorts_numerically(self):
        versions = ["UE_5.10", "UE_4.27", "UE_5.9", "UE_5.2"]

        self.assertEqual(sorted(versions, key=engine_sort_key), ["UE_4.27", "UE_5.2", "UE_5.9", "UE_5.10"])


class LibraryItemTests(unittest.TestCase):
    def test_parses_library_fields(self):
        item = parse_library_item(_raw_item())

        self.assertEqual(item.asset_id, "asset1")
        self.assertEqual(item.namespace, "ns1")
        self.assertEqual(item.distribution_method, "CODE_PLUGIN")
        self.assertEqual(item.engine_versions, ["UE_5.1", "UE_5.2", "UE_5.3"])
        self.assertEqual(item.version_for("UE_5.1"), ProjectVersion("art52", ("UE_5.2", "UE_5.1")))
        self.assertIsNone(item.version_for("UE_5.4"))

    def test_tolerates_missing_versions(self):
        raw = _raw_item()
        raw["projectVersions"] = None
        raw["title"] = ""

        item = parse_library_item(raw)

        self.assertEqual(item.project_versions, ())
        self.assertEqual(item.title, "asset1")

    def test_finds_by_exact_id_then_title(self):
        items = [parse_library_item(_raw_item("a1", "Flow Graph")), parse_library_item(_raw_item("flow", "Other"))]

        self.assertEqual([i.asset_id for i in find_items(items, "flow")], ["flow"])
        self.assertEqual([i.asset_id for i in find_items(items, "GRAPH")], ["a1"])
        self.assertEqual(find_items(items, "missing"), [])


class ListLibraryTests(unittest.TestCase):
    def test_follows_cursors(self):
        http = FakeHttp(
            (200, {"cursors": {"next": "abc+/="}, "results": [_raw_item("a1")]}),
            (200, {"cursors": {"next": None}, "results": [_raw_item("a2")]}),
        )

        items = list_library(http, SESSION)

        self.assertEqual([i.asset_id for i in items], ["a1", "a2"])
        first, second = (urlparse(r["url"]) for r in http.requests)
        self.assertEqual(first.path, "/e/accounts/account1/ue/library")
        self.assertEqual(parse_qs(first.query), {"count": ["100"]})
        self.assertEqual(parse_qs(second.query), {"cursor": ["abc+/="], "count": ["100"]})
        self.assertEqual(http.requests[0]["headers"]["Authorization"], "Bearer token")


class RequestBuildTests(unittest.TestCase):
    def setUp(self):
        self.item = parse_library_item(_raw_item())
        self.version = self.item.version_for("UE_5.3")

    def _info(self, artifact_id="art53"):
        return {
            "artifactId": artifact_id,
            "buildVersion": f"{artifact_id}-build",
            "distributionPointBaseUrls": ["https://cdn-a/Builds/x/", "https://cdn-b/Builds/x/", "https://cdn-c/x/"],
            "distributionPoints": [
                {"manifestUrl": "https://cdn-b/Builds/x/m.manifest?sig=2", "signatureExpiration": "2030-01-01T00:00:00Z"},
                {"manifestUrl": "https://cdn-a/Builds/x/m.manifest?sig=1", "signatureExpiration": "2030-01-01T00:00:00Z"},
            ],
        }

    def test_requests_the_artifact_manifest(self):
        http = FakeHttp((200, {"downloadInfo": [self._info("other"), self._info()]}))

        build = request_build(http, SESSION, self.item, self.version)

        request = http.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "https://www.fab.com/e/artifacts/art53/manifest")
        self.assertEqual(json.loads(request["body"]), {"item_id": "asset1", "namespace": "ns1", "platform": "Windows"})
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(build.build_version, "art53-build")
        self.assertEqual(build.sources[0], DownloadSource("https://cdn-a/Builds/x/m.manifest?sig=1", "https://cdn-a/Builds/x"))

    def test_pairs_each_cdn_with_its_manifest(self):
        self.assertEqual(
            pair_sources(self._info()),
            [
                DownloadSource("https://cdn-a/Builds/x/m.manifest?sig=1", "https://cdn-a/Builds/x"),
                DownloadSource("https://cdn-b/Builds/x/m.manifest?sig=2", "https://cdn-b/Builds/x"),
            ],
        )

    def test_rejects_info_without_locations(self):
        with self.assertRaises(FabError):
            pair_sources({"distributionPointBaseUrls": ["https://cdn"], "distributionPoints": []})

    def test_rejects_an_empty_answer(self):
        with self.assertRaisesRegex(FabError, "no download information"):
            request_build(FakeHttp((200, {"downloadInfo": []})), SESSION, self.item, self.version)

    def test_explains_http_403(self):
        with self.assertRaisesRegex(FabError, "403"):
            request_build(FakeHttp((403, b"")), SESSION, self.item, self.version)

    def test_passes_other_errors_through(self):
        with self.assertRaises(HttpError):
            request_build(FakeHttp((500, b"")), SESSION, self.item, self.version)


if __name__ == "__main__":
    unittest.main()
