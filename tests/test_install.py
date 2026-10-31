# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fabdl import install as install_module
from fabdl.install import (
    RECORD_FILE,
    InstallError,
    InstallRecord,
    PluginTarget,
    add_content,
    install_plugin,
    installed_plugins,
    is_enabled,
    plugin_folders,
)
from fabdl.project import load_project, read_engine

from .fakes import make_plugin, make_project

RECORD = InstallRecord("asset1", "ns1", "art53", "Flow Graph", "UE_5.3", "1.0+build", "Windows", "2026-10-29T12:00:00+00:00")


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.download = self.root / "download"

    def _project(self, *, installed_engine=False):
        return load_project(make_project(Path(tempfile.mkdtemp(dir=self.root)), installed_engine=installed_engine))

    def _target(self, **kwargs):
        project = self._project(**kwargs)
        return project, PluginTarget.for_project(project)


class ProjectPluginTests(InstallTestCase):
    def test_installs_a_new_plugin_without_prebuilt_folders_for_source_engines(self):
        project, target = self._target()
        make_plugin(self.download / "FlowGraph-5.3", "FlowGraph")

        result = install_plugin(self.download, target, RECORD)

        folder = project.plugins_dir / "FlowGraph"
        self.assertEqual(result.plugin.folder, folder)
        self.assertTrue((folder / "FlowGraph.uplugin").is_file())
        self.assertTrue((folder / "Source" / "FlowGraph" / "FlowGraph.cpp").is_file())
        self.assertFalse((folder / "Binaries").exists())
        self.assertFalse((folder / "Intermediate").exists())
        self.assertTrue(result.removed_binaries)
        self.assertIsNone(result.backup)
        self.assertTrue(result.enabled)
        self.assertEqual(InstallRecord.from_json((folder / RECORD_FILE).read_text()), RECORD)

    def test_keeps_prebuilt_folders_for_installed_engines(self):
        _, target = self._target(installed_engine=True)
        make_plugin(self.download, "FlowGraph")

        result = install_plugin(self.download, target, RECORD)

        self.assertTrue((result.plugin.folder / "Binaries" / "Win64").is_dir())
        self.assertFalse(result.removed_binaries)

    def test_reports_plugins_the_project_will_not_load(self):
        _, target = self._target()
        make_plugin(self.download, "FlowGraph")
        (self.download / "FlowGraph.uplugin").write_text('{"EnabledByDefault": false}')

        self.assertFalse(install_plugin(self.download, target, RECORD).enabled)

    def test_replaces_an_existing_copy_where_it_is_and_backs_it_up(self):
        project, target = self._target()
        old = make_plugin(project.plugins_dir / "Marketplace" / "Flow", "FlowGraph", binaries=False)
        (old / "old.txt").write_text("old")
        make_plugin(self.download / "FlowGraph", "FlowGraph")

        result = install_plugin(self.download, target, RECORD)

        self.assertEqual(result.plugin.folder, old)
        self.assertFalse((old / "old.txt").exists())
        self.assertTrue((old / RECORD_FILE).is_file())
        self.assertEqual(result.backup.parent, project.root / "Saved" / "PluginBackups")
        self.assertTrue(result.backup.name.startswith("Flow-"))
        self.assertEqual((result.backup / "old.txt").read_text(), "old")

    def test_rejects_downloads_that_are_not_one_plugin(self):
        _, target = self._target()
        (self.download / "Content").mkdir(parents=True)

        with self.assertRaisesRegex(InstallError, "not a code plugin"):
            install_plugin(self.download, target, RECORD)

        make_plugin(self.download / "A", "A")
        make_plugin(self.download / "B", "B")
        with self.assertRaisesRegex(InstallError, "contains 2 plugins"):
            install_plugin(self.download, target, RECORD)

    def test_rejects_several_copies_in_the_project(self):
        project, target = self._target()
        make_plugin(project.plugins_dir / "One", "FlowGraph")
        make_plugin(project.plugins_dir / "Two", "FlowGraph")
        make_plugin(self.download, "FlowGraph")

        with self.assertRaisesRegex(InstallError, "several copies"):
            install_plugin(self.download, target, RECORD)

    def test_leaves_the_old_copy_when_it_is_locked(self):
        project, target = self._target()
        old = make_plugin(project.plugins_dir / "FlowGraph", "FlowGraph")
        make_plugin(self.download, "FlowGraph")

        with mock.patch.object(install_module.os, "rename", side_effect=PermissionError("in use")):
            with self.assertRaisesRegex(InstallError, "Close the Unreal Editor"):
                install_plugin(self.download, target, RECORD)

        self.assertTrue((old / "Binaries").is_dir())

    def test_restores_the_old_copy_when_the_move_fails(self):
        project, target = self._target()
        old = make_plugin(project.plugins_dir / "FlowGraph", "FlowGraph")
        make_plugin(self.download, "FlowGraph")

        with mock.patch.object(install_module.shutil, "move", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(InstallError, "disk full"):
                install_plugin(self.download, target, RECORD)

        self.assertTrue((old / "Binaries").is_dir())
        self.assertFalse((old / RECORD_FILE).exists())

    def test_backups_never_collide(self):
        path = self.root / "Backup"
        path.mkdir()
        (self.root / "Backup-2").mkdir()

        self.assertEqual(install_module._unused_path(path), self.root / "Backup-3")


class EnginePluginTests(InstallTestCase):
    def _engine(self, *, installed_engine=False):
        root = Path(tempfile.mkdtemp(dir=self.root))
        make_project(root, installed_engine=installed_engine)
        return read_engine(root)

    def test_installs_into_the_marketplace_folder(self):
        engine = self._engine()
        target = PluginTarget.for_engine(engine)
        make_plugin(self.download, "FlowGraph")

        result = install_plugin(self.download, target, RECORD)

        folder = engine.root / "Engine" / "Plugins" / "Marketplace" / "FlowGraph"
        self.assertEqual(result.plugin.folder, folder)
        self.assertTrue(result.removed_binaries)
        self.assertEqual(installed_plugins(target.plugins_dir)[0].record, RECORD)
        self.assertEqual(target.name, "the UE_5.3 engine")

    def test_engine_plugins_load_only_when_enabled_by_default(self):
        engine = self._engine(installed_engine=True)
        make_plugin(self.download, "FlowGraph")

        result = install_plugin(self.download, PluginTarget.for_engine(engine), RECORD)

        self.assertFalse(result.enabled)
        self.assertFalse(result.removed_binaries)

    def test_never_replaces_a_built_in_engine_plugin(self):
        engine = self._engine()
        built_in = make_plugin(engine.root / "Engine" / "Plugins" / "Runtime" / "FlowGraph", "FlowGraph")
        make_plugin(self.download, "FlowGraph")

        with self.assertRaisesRegex(InstallError, "will not replace"):
            install_plugin(self.download, PluginTarget.for_engine(engine), RECORD)

        self.assertTrue((built_in / "Binaries").is_dir())


class IsEnabledTests(InstallTestCase):
    def _case(self, uproject_plugins, descriptor):
        project = self._project()
        project.path.write_text(json.dumps({"EngineAssociation": "", "Plugins": uproject_plugins}))
        plugin = project.plugins_dir / "FlowGraph" / "FlowGraph.uplugin"
        plugin.parent.mkdir(parents=True)
        plugin.write_text(json.dumps(descriptor))
        return is_enabled(PluginTarget.for_project(project), plugin)

    def test_project_plugins_are_enabled_by_default(self):
        self.assertTrue(self._case([], {}))
        self.assertTrue(self._case([], {"EnabledByDefault": True}))
        self.assertFalse(self._case([], {"EnabledByDefault": False}))

    def test_the_project_list_decides(self):
        self.assertTrue(self._case([{"Name": "flowgraph", "Enabled": True}], {"EnabledByDefault": False}))
        self.assertFalse(self._case([{"Name": "FlowGraph", "Enabled": False}], {}))
        self.assertTrue(self._case([{"Name": "Other", "Enabled": False}], {}))


class AddContentTests(InstallTestCase):
    def _pack(self, layout_root):
        content = self.download / layout_root
        (content / "RockPack" / "Meshes").mkdir(parents=True)
        (content / "RockPack" / "Meshes" / "Rock.uasset").write_text("new rock")
        (content / "RockPack" / "Map.umap").write_text("map")
        return content

    def test_merges_the_pack_into_the_projects_content(self):
        project = self._project()
        self._pack("Content")
        (self.download / "manifest.txt").write_text("outside Content")

        result = add_content(self.download, project)

        content = project.root / "Content"
        self.assertEqual((content / "RockPack" / "Meshes" / "Rock.uasset").read_text(), "new rock")
        self.assertEqual(result.folders, [content / "RockPack"])
        self.assertEqual((result.file_count, result.skipped, result.backup), (2, 1, None))

    def test_finds_content_one_level_down(self):
        project = self._project()
        self._pack("RockPack/Content")

        add_content(self.download, project)

        self.assertTrue((project.root / "Content" / "RockPack" / "Map.umap").is_file())

    def test_backs_up_files_it_replaces(self):
        project = self._project()
        existing = project.root / "Content" / "RockPack" / "Map.umap"
        existing.parent.mkdir(parents=True)
        existing.write_text("my edits")
        (project.root / "Content" / "Mine.uasset").write_text("untouched")
        self._pack("Content")

        result = add_content(self.download, project)

        self.assertEqual(existing.read_text(), "map")
        self.assertEqual((result.backup / "RockPack" / "Map.umap").read_text(), "my edits")
        self.assertEqual((project.root / "Content" / "Mine.uasset").read_text(), "untouched")

    def test_undoes_everything_when_a_file_is_locked(self):
        project = self._project()
        existing = project.root / "Content" / "RockPack" / "Map.umap"
        existing.parent.mkdir(parents=True)
        existing.write_text("my edits")
        self._pack("Content")
        real_replace = install_module.os.replace
        calls = []

        def flaky_replace(source, destination):
            calls.append(source)
            if len(calls) == 3:  # After the backup and one new file.
                raise PermissionError("locked by the editor")
            real_replace(source, destination)

        with mock.patch.object(install_module.os, "replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(InstallError, "Nothing was changed"):
                add_content(self.download, project)

        self.assertEqual(existing.read_text(), "my edits")
        self.assertEqual(
            sorted(p.relative_to(project.root).as_posix() for p in (project.root / "Content").rglob("*") if p.is_file()),
            ["Content/RockPack/Map.umap"],
        )

    def test_rejects_complete_projects_and_unknown_downloads(self):
        project = self._project()
        (self.download / "Sample").mkdir(parents=True)
        (self.download / "Sample" / "Sample.uproject").write_text("{}")
        (self.download / "Sample" / "Content").mkdir()

        with self.assertRaisesRegex(InstallError, "complete project"):
            add_content(self.download, project)

        other = self.root / "other"
        (other / "Docs").mkdir(parents=True)
        with self.assertRaisesRegex(InstallError, "no plugin and no Content folder"):
            add_content(other, project)


class InstalledPluginsTests(InstallTestCase):
    def test_lists_only_plugins_with_a_record(self):
        project = self._project()
        tracked = make_plugin(project.plugins_dir / "Marketplace" / "FlowGraph", "FlowGraph")
        (tracked / RECORD_FILE).write_text(RECORD.to_json())
        make_plugin(project.plugins_dir / "Manual", "Manual")
        damaged = make_plugin(project.plugins_dir / "Damaged", "Damaged")
        (damaged / RECORD_FILE).write_text("{}")

        plugins = installed_plugins(project.plugins_dir)

        self.assertEqual([(p.folder, p.record) for p in plugins], [(tracked, RECORD)])

    def test_does_not_look_inside_plugins(self):
        outer = make_plugin(self.root / "Plugins" / "Outer", "Outer")
        make_plugin(outer / "Source" / "Nested", "Nested")

        self.assertEqual(list(plugin_folders(self.root / "Plugins")), [outer])

    def test_a_missing_folder_has_no_plugins(self):
        self.assertEqual(installed_plugins(self.root / "Missing"), [])


if __name__ == "__main__":
    unittest.main()
