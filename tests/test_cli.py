import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fabdl import cli
from fabdl.cli import UsageError, _choose_engine, _choose_item, main
from fabdl.fab import LibraryItem, ProjectVersion

from .fakes import make_project


def _item(asset_id, title, *engines):
    return LibraryItem(asset_id, "ns", title, "CODE_PLUGIN", (ProjectVersion("art", tuple(engines)),))


ITEMS = [_item("a1", "Flow Graph", "UE_5.3"), _item("a2", "Flow Graph Extras", "UE_5.2", "UE_5.3")]


class ChooseTests(unittest.TestCase):
    def test_returns_the_single_match(self):
        self.assertEqual(_choose_item(ITEMS, "extras", interactive=False).asset_id, "a2")
        self.assertEqual(_choose_item(ITEMS, "a1", interactive=False).asset_id, "a1")

    def test_lists_several_matches_when_not_interactive(self):
        with self.assertRaisesRegex(UsageError, r"2 items match(.|\n)*Flow Graph \(id a1\)"):
            _choose_item(ITEMS, "flow", interactive=False)

    def test_reports_no_match(self):
        with self.assertRaisesRegex(UsageError, "No Unreal Engine item"):
            _choose_item(ITEMS, "voxel", interactive=False)

    def test_uses_the_only_engine_version(self):
        self.assertEqual(_choose_engine(ITEMS[0], interactive=False), "UE_5.3")

    def test_asks_for_an_engine_when_not_interactive(self):
        with self.assertRaisesRegex(UsageError, "Pass --engine.*UE_5.2, UE_5.3"):
            _choose_engine(ITEMS[1], interactive=False)


class MainTests(unittest.TestCase):
    def _main(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_rejects_a_bad_engine_version_before_signing_in(self):
        code, _, errors = self._main("download", "flow", "--engine", "latest")

        self.assertEqual(code, 1)
        self.assertIn("Engine version must look like 5.3", errors)

    def test_project_commands_need_a_project(self):
        with mock.patch.object(cli.Settings, "load", return_value=cli.Settings()):
            code, _, errors = self._main("outdated")

        self.assertEqual(code, 1)
        self.assertIn("Pass --project", errors)

    def test_outdated_with_nothing_installed_needs_no_sign_in(self):
        with tempfile.TemporaryDirectory() as temp:
            uproject = make_project(Path(temp))

            code, output, _ = self._main("outdated", "--project", str(uproject))

        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "Game has no plugins installed by fabdl.")

    def test_outdated_for_an_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            make_project(Path(temp))

            code, output, _ = self._main("outdated", "--engine-dir", temp)

        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "The UE_5.3 engine has no plugins installed by fabdl.")

    def test_project_and_engine_are_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["install", "x", "--project", "a.uproject", "--engine-dir", "b"])


if __name__ == "__main__":
    unittest.main()
