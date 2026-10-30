import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
