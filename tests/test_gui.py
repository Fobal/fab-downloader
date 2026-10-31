# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from fabdl import gui
from fabdl.download import DownloadCancelled
from fabdl.gui import ENGINE, FOLDER, PROJECT, App, default_engine, login_code_from_clipboard
from fabdl.install import RECORD_FILE, ContentResult, InstalledPlugin, InstallRecord, InstallResult, PluginTarget
from fabdl.manifest import Manifest
from fabdl.project import read_engine
from fabdl.settings import Settings
from fabdl.workflow import UpdateStatus

from .fakes import FakeHttp, MemoryStore, make_plugin, make_project, token_response

ALL_COLUMNS = ("title", "type", "installed")
FOLDER_COLUMNS = ("title", "type")


def _library_page():
    def item(asset_id, title, *engines):
        return {
            "assetId": asset_id,
            "assetNamespace": "ns",
            "title": title,
            "distributionMethod": "CODE_PLUGIN",
            "projectVersions": [{"artifactId": f"{asset_id}-{e}", "engineVersions": [e]} for e in engines],
        }

    return {"cursors": {}, "results": [
        item("a1", "Flow Graph", "UE_5.2", "UE_5.3"),
        item("a2", "Logic Driver", "UE_5.1", "UE_5.2"),
        item("a3", "Megascans Rock"),
    ]}


def _record(asset_id="a1", engine="UE_5.3", build="1.0"):
    return InstallRecord(asset_id, "ns", f"{asset_id}-{engine}", "Flow Graph", engine, build, "Windows", "")


class DefaultEngineTests(unittest.TestCase):
    def test_keeps_the_preferred_version_when_available(self):
        self.assertEqual(default_engine(["UE_5.2", "UE_5.3"], "UE_5.2"), "UE_5.2")

    def test_falls_back_to_the_newest(self):
        self.assertEqual(default_engine(["UE_5.2", "UE_5.3"], "UE_4.27"), "UE_5.3")
        self.assertEqual(default_engine(["UE_5.2", "UE_5.3"], None), "UE_5.3")


class AppTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.errors = []
        self._patch(gui.messagebox, "showerror", side_effect=lambda title, message, **kwargs: self.errors.append(message))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.settings_path = self.temp / "settings.json"

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _app(self, http, store, engines=()):
        app = App(
            self.root, http=http, store=store, settings_path=self.settings_path,
            start_thread=lambda task: task(), engines=list(engines),
        )
        self._settle(app)
        return app

    def _signed_in_app(self, engines=()):
        return self._app(FakeHttp((200, _library_page())), MemoryStore(token_response()), engines)

    def _project_app(self, *, installed=(), engine_version=""):
        """Signed in with Project selected, and a saved project whose Plugins hold the given install records."""
        uproject = make_project(self.temp / "engine")
        for record in installed:
            folder = make_plugin(uproject.parent / "Plugins" / record.asset_id, record.asset_id)
            (folder / RECORD_FILE).write_text(record.to_json())
        Settings(destination=PROJECT, project=str(uproject), engine_version=engine_version).save(self.settings_path)
        return self._signed_in_app()

    def _settle(self, app):
        app.process_events()
        self.root.update()

    def _rows(self, app):
        return [tuple(app.tree.item(row, "values")) for row in app.tree.get_children()]

    def _titles(self, app):
        return [values[0] for values in self._rows(app)]

    def _select(self, app, title):
        row = next(row for row in app.tree.get_children() if app.tree.item(row, "values")[0] == title)
        app.tree.selection_set(row)
        self.root.update()

    def _switch(self, app, destination):
        app.destination_var.set(destination)
        app._destination_changed()

    def _shown(self, widget):
        return widget.winfo_manager() == "grid"

    def _enabled(self, widget):
        return "disabled" not in widget.state()


class LibraryTests(AppTestCase):
    def test_shows_the_library_with_folder_selected_by_default(self):
        app = self._signed_in_app()

        self.assertEqual(app.account_label["text"], "Signed in as Me")
        self.assertFalse(self._shown(app.sign_in_frame))
        self.assertEqual(self._titles(app), ["Flow Graph", "Logic Driver"])
        self.assertEqual(app.status_var.get(), "2 items with Unreal Engine builds.")
        self.assertEqual(app.destination_var.get(), FOLDER)
        self.assertEqual(app.action_button["text"], "Download")
        self.assertEqual(tuple(app.tree["displaycolumns"]), FOLDER_COLUMNS)
        self.assertFalse(self._shown(app.updates_button))
        self.assertFalse(self._enabled(app.action_button), "no item selected")
        self.assertFalse(self._enabled(app.project_button))

    def test_search_filters_the_list(self):
        app = self._signed_in_app()

        app.search_var.set("logic")

        self.assertEqual(self._titles(app), ["Logic Driver"])

    def test_remembers_the_engine_picked_for_folder_downloads(self):
        app = self._signed_in_app()

        self._select(app, "Flow Graph")
        self.assertEqual(app.engine_var.get(), "UE_5.3")
        self.assertEqual(list(app.engine_box["values"]), ["UE_5.2", "UE_5.3"])
        self.assertTrue(self._enabled(app.action_button))
        app.engine_var.set("UE_5.2")
        app._engine_selected()
        self._select(app, "Logic Driver")

        self.assertEqual(app.engine_var.get(), "UE_5.2")
        self.assertEqual(Settings.load(self.settings_path).engine_version, "UE_5.2")

    def test_keeps_the_engine_choice_when_the_list_is_redrawn(self):
        app = self._signed_in_app()
        self._select(app, "Flow Graph")
        app.engine_var.set("UE_5.2")

        app.search_var.set("flow")
        self.root.update()

        self.assertEqual(app.engine_var.get(), "UE_5.2")

    def test_reports_failures_and_stays_usable(self):
        app = self._app(FakeHttp((500, b"")), MemoryStore(token_response()))

        self.assertEqual(len(self.errors), 1)
        self.assertIn("HTTP 500", self.errors[0])
        self.assertTrue(app.status_var.get().startswith("Error: "))
        self.assertTrue(self._enabled(app.refresh_button))

    def test_names_unexpected_errors(self):
        with mock.patch.object(gui.workflow, "unreal_library", side_effect=RuntimeError("boom")):
            self._app(FakeHttp(), MemoryStore(token_response()))

        self.assertEqual(self.errors, ["RuntimeError: boom"])


class SessionTests(AppTestCase):
    def test_signs_in_with_the_pasted_code(self):
        store = MemoryStore()
        app = self._app(FakeHttp((200, token_response()), (200, _library_page())), store)
        self.assertTrue(self._shown(app.sign_in_frame))
        self.assertEqual(app.status_var.get(), "Sign in to see your Fab library.")

        app.code_var.set('{"authorizationCode":"abc123"}')
        app._sign_in()
        self._settle(app)

        self.assertFalse(self._shown(app.sign_in_frame))
        self.assertEqual(app.code_var.get(), "")
        self.assertEqual(store.data["access_token"], "access1")
        self.assertEqual(self._titles(app), ["Flow Graph", "Logic Driver"])

    def test_shows_sign_in_errors(self):
        app = self._app(FakeHttp(), MemoryStore())

        app.code_var.set("not a code")
        app._sign_in()
        self._settle(app)

        self.assertEqual(len(self.errors), 1)
        self.assertIn("authorizationCode", self.errors[0])
        self.assertTrue(self._shown(app.sign_in_frame))

    def test_returns_to_sign_in_when_the_session_ends(self):
        store = MemoryStore(token_response())
        app = self._app(FakeHttp((200, _library_page())), store)

        store.clear()
        app._load_library()
        self._settle(app)

        self.assertTrue(self._shown(app.sign_in_frame))
        self.assertEqual(self._rows(app), [])
        self.assertIn("session ended", self.errors[0])

    def test_signs_out(self):
        store = MemoryStore(token_response())
        app = self._app(FakeHttp((200, _library_page()), (204, b"")), store)

        app._sign_out()
        self._settle(app)

        self.assertIsNone(store.data)
        self.assertEqual(app.status_var.get(), "Signed out.")
        self.assertTrue(self._shown(app.sign_in_frame))

    def test_sponsor_opens_the_sponsor_section_even_when_signed_out(self):
        app = self._app(FakeHttp(), MemoryStore())
        browser = self._patch(gui.webbrowser, "open")

        app.sponsor_button.invoke()

        browser.assert_called_once_with("https://github.com/Fobal/fab-downloader#sponsor")

    def test_repo_link_opens_the_repository(self):
        app = self._app(FakeHttp(), MemoryStore())
        browser = self._patch(gui.webbrowser, "open")

        app.repo_button.invoke()

        browser.assert_called_once_with("https://github.com/Fobal/fab-downloader")


class DestinationTests(AppTestCase):
    def test_opens_the_saved_project_and_shows_installed_plugins(self):
        app = self._project_app(installed=[_record()])

        self.assertTrue(app.project_var.get().endswith("Game.uproject"))
        self.assertIn("UE_5.3", app.engine_label["text"])
        self.assertIn("source build", app.engine_label["text"])
        self.assertEqual(app.action_button["text"], "Add to project")
        self.assertEqual(tuple(app.tree["displaycolumns"]), ALL_COLUMNS)
        self.assertEqual(self._rows(app)[0], ("Flow Graph", "Code plugin", "UE_5.3"))
        self.assertTrue(self._shown(app.updates_button))
        self.assertTrue(self._enabled(app.updates_button))
        self.assertFalse(self._enabled(app.folder_entry))

    def test_hides_the_update_check_until_a_plugin_is_installed(self):
        app = self._project_app()

        self.assertEqual(tuple(app.tree["displaycolumns"]), ALL_COLUMNS)
        self.assertFalse(self._shown(app.updates_button))

    def test_switching_to_folder_hides_the_project_parts(self):
        app = self._project_app(installed=[_record()], engine_version="UE_5.2")
        self._select(app, "Flow Graph")
        self.assertEqual(app.engine_var.get(), "UE_5.3", "the project's engine")

        self._switch(app, FOLDER)

        self.assertEqual(app.action_button["text"], "Download")
        self.assertEqual(tuple(app.tree["displaycolumns"]), FOLDER_COLUMNS)
        self.assertFalse(self._shown(app.updates_button))
        self.assertTrue(self._enabled(app.folder_entry))
        self.assertFalse(self._enabled(app.project_button))
        self.assertEqual(app.engine_var.get(), "UE_5.2", "the engine remembered for folder downloads")
        self.assertEqual(Settings.load(self.settings_path).destination, FOLDER)

    def test_showing_the_installed_column_keeps_the_window_size(self):
        self.root.deiconify()  # Tk only resizes a window to fit its widgets while it is shown.
        app = self._signed_in_app()
        size = self.root.geometry().split("+")[0]

        self._switch(app, PROJECT)
        self.root.update()

        self.assertEqual(self.root.geometry().split("+")[0], size)
        widths = sum(app.tree.column(column, "width") for column in ALL_COLUMNS)
        self.assertLessEqual(widths, app.tree.winfo_width(), "the Installed column fits in the list")

    def test_project_without_a_project_asks_for_one(self):
        app = self._signed_in_app()
        self._select(app, "Flow Graph")

        self._switch(app, PROJECT)

        self.assertFalse(self._enabled(app.action_button))
        self.assertTrue(self._enabled(app.project_button))
        self.assertIn("Choose your .uproject", app.status_var.get())

    def test_switching_away_from_project_drops_the_hint(self):
        app = self._signed_in_app()
        self._switch(app, PROJECT)

        self._switch(app, FOLDER)

        self.assertEqual(app.status_var.get(), "2 items with Unreal Engine builds.")

    def test_chooses_a_project_and_remembers_it(self):
        uproject = make_project(self.temp / "engine")
        app = self._signed_in_app()
        self._select(app, "Flow Graph")
        self._switch(app, PROJECT)

        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(uproject)):
            app._choose_project()

        self.assertEqual(app.project.name, "Game")
        self.assertTrue(self._enabled(app.action_button))
        self.assertEqual(app.status_var.get(), "2 items with Unreal Engine builds.", "the hint is gone")
        saved = Settings.load(self.settings_path)
        self.assertEqual((saved.destination, saved.project), (PROJECT, str(uproject.resolve())))

    def test_rejects_a_broken_project_file(self):
        broken = self.temp / "Broken.uproject"
        broken.write_text("not json")
        app = self._signed_in_app()
        self._switch(app, PROJECT)

        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(broken)):
            app._choose_project()

        self.assertIsNone(app.project)
        self.assertIn("Could not read", self.errors[0])

    def test_reports_a_saved_project_that_is_gone(self):
        Settings(destination=PROJECT, project=str(self.temp / "Gone.uproject")).save(self.settings_path)

        app = self._signed_in_app()
        self._select(app, "Flow Graph")

        self.assertIsNone(app.project)
        self.assertIn("Could not open the last project", app.engine_label["text"])
        self.assertFalse(self._enabled(app.action_button))

    def test_defaults_to_the_projects_engine(self):
        app = self._project_app()

        self._select(app, "Flow Graph")
        self.assertEqual(app.engine_var.get(), "UE_5.3")
        self._select(app, "Logic Driver")
        self.assertEqual(app.engine_var.get(), "UE_5.2", "newest when the project's engine is missing")
        self.assertTrue(self._enabled(app.action_button))


class InstallTests(AppTestCase):
    def test_installs_and_shows_the_new_plugin(self):
        app = self._project_app()
        self._select(app, "Flow Graph")
        calls = []

        def fake_install(http, session, item, version, engine, project, *, on_manifest, report, stop):
            calls.append((item.asset_id, version.artifact_id, engine))
            on_manifest(Manifest(18, "1.0", {}, ()))
            report(1, 2)
            record = _record(build="2.0")
            folder = make_plugin(project.plugins_dir / "FlowGraph", "FlowGraph", binaries=False)
            (folder / RECORD_FILE).write_text(record.to_json())
            target = PluginTarget.for_project(project)
            return InstallResult(target, InstalledPlugin(folder, record), project.root / "Saved" / "old", True, False)

        with mock.patch.object(gui.workflow, "add_to_project", side_effect=fake_install):
            app._start_action()
            self._settle(app)

        folder = app.project.plugins_dir / "FlowGraph"
        self.assertEqual(calls, [("a1", "a1-UE_5.3", "UE_5.3")])
        self.assertTrue(self._shown(app.updates_button), "there is now a plugin to check")
        self.assertEqual(
            app.status_var.get().splitlines(),
            [
                f"Installed Flow Graph (UE_5.3) into {folder}",
                f"The previous version was moved to {app.project.root / 'Saved' / 'old'}",
                "Build the project to compile the plugin.",
                "The plugin is disabled in Game. Enable it in Edit > Plugins.",
            ],
        )
        self.assertEqual(app.result_folder, folder)
        self.assertTrue(self._shown(app.open_button))
        self.assertEqual(self._rows(app)[0][2], "UE_5.3 - up to date")
        self.assertEqual(app.engine_var.get(), "UE_5.3")

    def test_asks_before_installing_another_engines_build(self):
        app = self._project_app()
        self._select(app, "Logic Driver")

        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask, \
                mock.patch.object(gui.workflow, "add_to_project") as add_to_project:
            app._start_action()

        self.assertIn("uses UE_5.3", ask.call_args.args[1])
        add_to_project.assert_not_called()

    def test_checks_installed_plugins_for_updates(self):
        app = self._project_app(installed=[_record()])
        statuses = [UpdateStatus(_record(), "a1-UE_5.3", "1.1")]

        with mock.patch.object(gui.workflow, "check_updates", return_value=statuses) as check:
            app._check_updates()
            self._settle(app)

        self.assertEqual([r.asset_id for r in check.call_args.args[2]], ["a1"])
        self.assertEqual(self._rows(app)[0][2], "UE_5.3 - update available")
        self.assertEqual(app.status_var.get(), "1 update available. Select the plugin and install it again.")


class DownloadTests(AppTestCase):
    def test_downloads_into_the_folder_and_remembers_it(self):
        app = self._signed_in_app()
        app.folder_var.set(str(self.temp))
        self._select(app, "Flow Graph")
        out_dir = self.temp / "Flow Graph UE_5.3"

        def fake_download(http, session, item, version, target, *, on_manifest, report, stop):
            self.assertEqual(target, out_dir)
            report(3, 4)
            return [target / "FlowGraph" / "FlowGraph.uplugin", target / "FlowGraph" / "Source" / "A.cpp"]

        with mock.patch.object(gui.workflow, "download_item", side_effect=fake_download):
            app._start_action()
            self._settle(app)

        self.assertEqual(app.status_var.get(), f"Saved to {out_dir}\nPlugin folder: {out_dir / 'FlowGraph'}")
        self.assertEqual(app.result_folder, out_dir / "FlowGraph")
        self.assertEqual(app.progress["value"], 1)
        self.assertEqual(Settings.load(self.settings_path).download_folder, str(self.temp))

    def test_refuses_an_existing_output_folder(self):
        app = self._signed_in_app()
        app.folder_var.set(str(self.temp))
        self._select(app, "Flow Graph")
        (self.temp / "Flow Graph UE_5.3").mkdir()

        with mock.patch.object(gui.workflow, "download_item") as download_item:
            app._start_action()

        download_item.assert_not_called()
        self.assertIn("already exists", self.errors[0])

    def test_needs_a_folder(self):
        app = self._signed_in_app()
        self._select(app, "Flow Graph")

        app.folder_var.set("  ")

        self.assertFalse(self._enabled(app.action_button))

    def test_browses_for_the_folder(self):
        app = self._signed_in_app()

        with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(self.temp)):
            app._browse_folder()

        self.assertEqual(app.folder_var.get(), str(self.temp))
        self.assertEqual(Settings.load(self.settings_path).download_folder, str(self.temp))

    def test_cancel_stops_the_download_without_an_error(self):
        app = self._signed_in_app()
        app.folder_var.set(str(self.temp))
        self._select(app, "Flow Graph")
        self.assertFalse(self._shown(app.cancel_button))

        def fake_download(http, session, item, version, target, *, on_manifest, report, stop):
            self.assertTrue(self._shown(app.cancel_button))
            self.assertFalse(self._shown(app.open_button))
            app.cancel_button.invoke()
            self.assertTrue(stop.is_set())
            self.assertEqual(app.status_var.get(), "Cancelling...")
            self.assertFalse(self._enabled(app.cancel_button), "it can only be clicked once")
            raise DownloadCancelled()

        with mock.patch.object(gui.workflow, "download_item", side_effect=fake_download):
            app._start_action()
            self._settle(app)

        self.assertEqual(app.status_var.get(), "Cancelled.")
        self.assertEqual(self.errors, [])
        self.assertFalse(self._shown(app.cancel_button))
        self.assertTrue(self._enabled(app.action_button))

    def test_quitting_during_a_download_says_the_progress_is_kept(self):
        app = self._signed_in_app()
        app.busy, app.stop_event = True, threading.Event()

        with mock.patch.object(gui.messagebox, "askokcancel", return_value=False) as ask:
            app._close()

        self.assertIn("starting the same download again continues from there", ask.call_args.args[1])


if __name__ == "__main__":
    unittest.main()


class EngineDestinationTests(AppTestCase):
    def _engines(self):
        engines = []
        for name, version in (("UE58", (5, 8)), ("UE53", (5, 3))):
            make_project(self.temp / name, version=version)
            engines.append(read_engine(self.temp / name))
        return engines

    def test_installs_to_the_chosen_engine_and_remembers_it(self):
        engines = self._engines()
        record = _record()
        marketplace = engines[1].root / "Engine" / "Plugins" / "Marketplace"
        folder = make_plugin(marketplace / "FlowGraph", "FlowGraph")
        (folder / RECORD_FILE).write_text(record.to_json())
        app = self._signed_in_app(engines)
        self._switch(app, ENGINE)
        self.assertEqual(app.action_button["text"], "Install to engine")
        self.assertEqual(app.selected_engine(), engines[0], "the newest engine first")

        app.engine_choice.current(1)
        app._engine_dir_selected()
        self._select(app, "Flow Graph")

        self.assertEqual(app.engine_var.get(), "UE_5.3", "the engine's version")
        self.assertEqual(self._rows(app)[0][2], "UE_5.3", "plugins installed in that engine")
        self.assertEqual(Settings.load(self.settings_path).engine_dir, str(engines[1].root))

        def fake_install(http, session, item, version, engine, *, on_manifest, report, stop):
            self.assertEqual(engine, engines[1])
            target = PluginTarget.for_engine(engine)
            return InstallResult(target, InstalledPlugin(folder, record), None, True, False)

        with mock.patch.object(gui.workflow, "install_to_engine", side_effect=fake_install):
            app._start_action()
            self._settle(app)

        self.assertEqual(
            app.status_var.get().splitlines(),
            [
                f"Installed Flow Graph (UE_5.3) into {folder}",
                "Build the project to compile the plugin.",
                "The plugin is off by default. Enable it in each project's Edit > Plugins.",
            ],
        )

    def test_no_engines_found(self):
        app = self._signed_in_app()
        self._select(app, "Flow Graph")

        self._switch(app, ENGINE)

        self.assertFalse(self._enabled(app.action_button))
        self.assertIn("No Unreal Engine installs", app.status_var.get())


class AssetPackTests(AppTestCase):
    def test_shows_what_an_asset_pack_added(self):
        app = self._project_app()
        self._select(app, "Flow Graph")

        def fake_add(http, session, item, version, engine, project, *, on_manifest, report, stop):
            folder = project.root / "Content" / "RockPack"
            return ContentResult(project, [folder], 12, None, 0)

        with mock.patch.object(gui.workflow, "add_to_project", side_effect=fake_add):
            app._start_action()
            self._settle(app)

        content = app.project.root / "Content"
        self.assertEqual(app.status_var.get(), f"Added 12 files to {content} (RockPack)")
        self.assertEqual(app.result_folder, content / "RockPack")


class ClipboardSignInTests(AppTestCase):
    PAGE = '{"warning":"Do not share","authorizationCode":"abc123","exchangeCode":null}'

    def test_recognizes_only_a_new_login_page(self):
        self.assertEqual(login_code_from_clipboard(self.PAGE, ""), "abc123")
        self.assertIsNone(login_code_from_clipboard(self.PAGE, self.PAGE), "copied before the login page opened")
        self.assertIsNone(login_code_from_clipboard("abc123", ""), "bare text is never sent to Epic")
        self.assertIsNone(login_code_from_clipboard('{"other": 1}', ""))
        self.assertIsNone(login_code_from_clipboard('{"authorizationCode": null}', ""))

    def test_signs_in_when_the_page_is_copied(self):
        store = MemoryStore()
        app = self._app(FakeHttp((200, token_response()), (200, _library_page())), store)
        clipboard = ["old text"]
        self._patch(app, "_clipboard_text", side_effect=lambda: clipboard[0])
        self._patch(gui.webbrowser, "open")

        app._open_login()
        self.assertIsNotNone(app.clipboard_job)
        app._watch_clipboard()
        self.assertIsNone(store.data, "nothing copied yet")

        clipboard[0] = self.PAGE
        app._watch_clipboard()
        self._settle(app)

        self.assertEqual(store.data["access_token"], "access1")
        self.assertFalse(self._shown(app.sign_in_frame))
        self.assertIsNone(app.clipboard_job, "watching stops after signing in")


class DestinationClickTests(AppTestCase):
    def test_clicking_the_project_field_picks_a_project(self):
        uproject = make_project(self.temp / "engine")
        app = self._signed_in_app()

        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(uproject)) as ask:
            app._project_field_clicked()

        ask.assert_called_once()
        self.assertEqual(app.destination, PROJECT)
        self.assertEqual(app.project.name, "Game")

    def test_clicking_a_greyed_out_row_selects_it(self):
        app = self._project_app()
        self.assertFalse(self._enabled(app.folder_entry))

        app._select_destination(FOLDER)

        self.assertEqual(app.destination, FOLDER)
        self.assertTrue(self._enabled(app.folder_entry))
        self.assertEqual(Settings.load(self.settings_path).destination, FOLDER)

    def test_clicks_are_ignored_while_busy(self):
        app = self._signed_in_app()
        app.busy = True

        with mock.patch.object(gui.filedialog, "askopenfilename") as ask:
            app._project_field_clicked()
        app._select_destination(ENGINE)

        ask.assert_not_called()
        self.assertEqual(app.destination, FOLDER)


class CustomEngineTests(AppTestCase):
    def test_adds_an_engine_folder_and_remembers_it(self):
        make_project(self.temp / "MyEngine", version=(5, 4))
        app = self._signed_in_app()

        with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(self.temp / "MyEngine" / "Engine")):
            app._browse_engine()

        self.assertEqual(app.destination, ENGINE)
        self.assertEqual(app.selected_engine().version, "UE_5.4")
        self.assertEqual(list(app.engine_choice["values"]), [app.selected_engine().describe()])
        saved = Settings.load(self.settings_path)
        self.assertEqual((saved.custom_engines, saved.engine_dir), ([str(self.temp / "MyEngine")], str(self.temp / "MyEngine")))

        second_root = tk.Tk()
        second_root.withdraw()
        self.addCleanup(second_root.destroy)
        reopened = App(
            second_root, http=FakeHttp((200, _library_page())), store=MemoryStore(token_response()),
            settings_path=self.settings_path, start_thread=lambda task: task(), engines=[],
        )
        self.assertEqual(reopened.selected_engine().root, self.temp / "MyEngine")

    def test_adding_a_listed_engine_selects_it(self):
        engines = []
        for name, version in (("A", (5, 8)), ("B", (5, 3))):
            make_project(self.temp / name, version=version)
            engines.append(read_engine(self.temp / name))
        app = self._signed_in_app(engines)

        with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(self.temp / "B")):
            app._browse_engine()

        self.assertEqual(len(app.engines), 2)
        self.assertEqual(app.selected_engine(), engines[1])
        self.assertEqual(Settings.load(self.settings_path).custom_engines, [])

    def test_rejects_a_folder_without_an_engine(self):
        app = self._signed_in_app()

        with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(self.temp)):
            app._browse_engine()

        self.assertIn("not an Unreal Engine folder", self.errors[0])
        self.assertEqual(app.engines, [])

    def test_skips_saved_engines_that_are_gone(self):
        Settings(custom_engines=[str(self.temp / "Deleted")]).save(self.settings_path)

        app = self._signed_in_app()

        self.assertEqual(app.engines, [])


class LoadingIndicatorTests(AppTestCase):
    def _deferred_app(self, http, store):
        """An app whose background tasks wait until run_tasks() is called."""
        tasks = []
        app = App(
            self.root, http=http, store=store, settings_path=self.settings_path,
            start_thread=tasks.append, engines=[],
        )

        def run_tasks():
            while tasks:
                tasks.pop(0)()
                self._settle(app)

        tasks.pop(0)()  # The saved-session check.
        app.process_events()
        return app, run_tasks

    def test_shows_over_the_list_while_the_library_loads(self):
        app, run_tasks = self._deferred_app(FakeHttp((200, _library_page())), MemoryStore(token_response()))

        self.assertEqual(app.loading_overlay.winfo_manager(), "place")
        run_tasks()

        self.assertEqual(app.loading_overlay.winfo_manager(), "")
        self.assertEqual(self._titles(app), ["Flow Graph", "Logic Driver"])

    def test_hides_when_loading_fails(self):
        app, run_tasks = self._deferred_app(FakeHttp((500, b"")), MemoryStore(token_response()))

        run_tasks()

        self.assertEqual(app.loading_overlay.winfo_manager(), "")
        self.assertIn("HTTP 500", self.errors[0])

    def test_hides_when_the_session_ended(self):
        store = MemoryStore(token_response())
        app, run_tasks = self._deferred_app(FakeHttp(), store)
        store.clear()

        run_tasks()

        self.assertEqual(app.loading_overlay.winfo_manager(), "")
        self.assertTrue(self._shown(app.sign_in_frame))
