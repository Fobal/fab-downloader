import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fabdl import project as project_module
from fabdl.project import Engine, ProjectError, engine_from_folder, find_engine_root, load_project, read_engine

from .fakes import make_project


def _no_builds():
    return {}


class ProjectTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)


class LoadProjectTests(ProjectTestCase):
    def test_finds_the_engine_around_a_project_without_association(self):
        uproject = make_project(self.root)

        project = load_project(uproject, registered_builds=_no_builds, launcher_installs=_no_builds)

        self.assertEqual(project.name, "Game")
        self.assertEqual(project.plugins_dir, uproject.parent.resolve() / "Plugins")
        self.assertEqual(project.engine, Engine(self.root.resolve(), "UE_5.3", False))

    def test_reads_registered_builds_by_guid(self):
        engine = self.root / "Engine58"
        make_project(engine, version=(5, 8))
        uproject = self.root / "Other" / "Other.uproject"
        uproject.parent.mkdir()
        uproject.write_text('\ufeff{"EngineAssociation": "{1EB80B8E-475B-BF68-1D3D-21834B964495}"}', encoding="utf-8")

        project = load_project(
            uproject,
            registered_builds=lambda: {"{1eb80b8e-475b-bf68-1d3d-21834b964495}": str(engine)},
            launcher_installs=_no_builds,
        )

        self.assertEqual(project.engine.version, "UE_5.8")
        self.assertEqual(project.engine.root, engine)

    def test_reads_launcher_installs_by_version(self):
        engine = self.root / "UE_5.3"
        make_project(engine, installed_engine=True)
        uproject = self.root / "Other.uproject"
        uproject.write_text(json.dumps({"EngineAssociation": "5.3"}))

        project = load_project(uproject, registered_builds=_no_builds, launcher_installs=lambda: {"5.3": str(engine)})

        self.assertTrue(project.engine.installed)
        self.assertEqual(project.engine.describe(), f"UE_5.3 at {engine} (installed build)")

    def test_reports_an_unknown_engine(self):
        uproject = self.root / "Other.uproject"
        uproject.write_text(json.dumps({"EngineAssociation": "5.9"}))

        with self.assertRaisesRegex(ProjectError, "5.9 is not installed"):
            load_project(uproject, registered_builds=_no_builds, launcher_installs=_no_builds)

    def test_reports_unreadable_projects(self):
        uproject = self.root / "Broken.uproject"
        uproject.write_text("not json")

        with self.assertRaisesRegex(ProjectError, "Could not read"):
            load_project(uproject)
        with self.assertRaisesRegex(ProjectError, "Could not read"):
            load_project(self.root / "Missing.uproject")


class EngineTests(ProjectTestCase):
    def test_requires_build_version(self):
        with self.assertRaisesRegex(ProjectError, "engine version"):
            read_engine(self.root)

    def test_project_outside_any_engine(self):
        with self.assertRaisesRegex(ProjectError, "not inside an engine folder"):
            find_engine_root("", self.root / "Lonely", _no_builds, _no_builds)

    def test_reads_the_launcher_install_list(self):
        listing = self.root / "LauncherInstalled.dat"
        listing.write_text(json.dumps({"InstallationList": [
            {"InstallLocation": "D:\\UE_5.3", "AppName": "UE_5.3"},
            {"InstallLocation": "D:\\Sample", "AppName": "StackOBot"},
            {"InstallLocation": "", "AppName": "UE_5.4"},
            "junk",
        ]}))

        with mock.patch.object(project_module, "LAUNCHER_LIST", listing):
            self.assertEqual(project_module._launcher_installs(), {"5.3": "D:\\UE_5.3"})

    def test_engine_from_a_picked_folder(self):
        make_project(self.root / "MyEngine")

        self.assertEqual(engine_from_folder(self.root / "MyEngine").root, self.root / "MyEngine")
        self.assertEqual(engine_from_folder(self.root / "MyEngine" / "Engine").root, self.root / "MyEngine")
        with self.assertRaisesRegex(ProjectError, "not an Unreal Engine folder"):
            engine_from_folder(self.root / "MyEngine" / "Games")

    def test_reads_engines_registered_in_install_ini(self):
        ini = self.root / "Install.ini"
        ini.write_text(
            "[Installations]\n{1EB80B8E-475B-BF68-1D3D-21834B964495}=/home/me/UnrealEngine\nEmpty=\n"
            "[Other]\nKey=/elsewhere\n"
        )

        with mock.patch.object(project_module, "INSTALL_INI", ini):
            self.assertEqual(
                project_module._install_ini_builds(),
                {"{1EB80B8E-475B-BF68-1D3D-21834B964495}": "/home/me/UnrealEngine"},
            )
        with mock.patch.object(project_module, "INSTALL_INI", self.root / "missing.ini"):
            self.assertEqual(project_module._install_ini_builds(), {})
        ini.write_text("no section header")
        with mock.patch.object(project_module, "INSTALL_INI", ini):
            self.assertEqual(project_module._install_ini_builds(), {})

    def test_missing_launcher_list_means_no_installs(self):
        with mock.patch.object(project_module, "LAUNCHER_LIST", self.root / "missing.dat"):
            self.assertEqual(project_module._launcher_installs(), {})


if __name__ == "__main__":
    unittest.main()
