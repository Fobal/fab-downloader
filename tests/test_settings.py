# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fabdl import settings as settings_module
from fabdl.settings import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "nested" / "settings.json"

    def test_round_trips(self):
        Settings(project="G:/Game/Game.uproject", download_folder="G:/Downloads", engine_version="UE_5.3").save(self.path)

        self.assertEqual(
            Settings.load(self.path),
            Settings(project="G:/Game/Game.uproject", download_folder="G:/Downloads", engine_version="UE_5.3"),
        )

    def test_defaults_when_missing_or_damaged(self):
        self.assertEqual(Settings.load(self.path), Settings())
        self.path.parent.mkdir()
        self.path.write_text("not json")
        self.assertEqual(Settings.load(self.path), Settings())
        self.path.write_text("[]")
        self.assertEqual(Settings.load(self.path), Settings())

    def test_ignores_unknown_and_mistyped_values(self):
        self.path.parent.mkdir()
        self.path.write_text(
            '{"project": 5, "download_folder": "D:/x", "future": "y", "custom_engines": ["E:/UE", 3], "engine_dir": ["x"]}'
        )

        self.assertEqual(Settings.load(self.path), Settings(download_folder="D:/x"))

    def test_round_trips_custom_engines(self):
        Settings(custom_engines=["E:/UE_5.3", "F:/MyEngine"]).save(self.path)

        self.assertEqual(Settings.load(self.path).custom_engines, ["E:/UE_5.3", "F:/MyEngine"])


class ConfigDirTests(unittest.TestCase):
    def test_uses_appdata_on_windows(self):
        with mock.patch.object(settings_module.sys, "platform", "win32"):
            with mock.patch.dict(settings_module.os.environ, {"APPDATA": "C:/Users/me/AppData/Roaming"}):
                self.assertEqual(settings_module._config_dir(), Path("C:/Users/me/AppData/Roaming"))

    def test_follows_xdg_on_linux(self):
        absolute = str(Path.home() / "xdg")
        with mock.patch.object(settings_module.sys, "platform", "linux"):
            with mock.patch.dict(settings_module.os.environ, {"XDG_CONFIG_HOME": absolute}):
                self.assertEqual(settings_module._config_dir(), Path(absolute))
            for value in ("", "relative/dir"):
                with mock.patch.dict(settings_module.os.environ, {"XDG_CONFIG_HOME": value}):
                    self.assertEqual(settings_module._config_dir(), Path.home() / ".config")


if __name__ == "__main__":
    unittest.main()
