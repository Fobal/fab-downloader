"""Window for browsing the Fab library and saving builds to a folder, a project or an engine.

Network work runs on a background thread. Results come back through a queue that the Tk
main loop drains, because Tk widgets may only be touched from the main thread.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Any, Callable

from . import auth, workflow
from .auth import Session, SessionStore
from .fab import LibraryItem, find_items
from .install import ContentResult, InstalledPlugin, InstallResult, PluginTarget, installed_plugins
from .manifest import Manifest
from .net import HttpClient
from .project import Engine, Project, ProjectError, engine_from_folder, find_engines, load_project
from .settings import SETTINGS_PATH, Settings
from .workflow import EXPECTED_ERRORS, UpdateStatus

TITLE = "fabdl"
FOLDER, PROJECT, ENGINE = "folder", "project", "engine"
_ACTION_LABELS = {FOLDER: "Download", PROJECT: "Add to project", ENGINE: "Install to engine"}
_POLL_MS = 50
_CLIPBOARD_POLL_MS = 500
_CLIPBOARD_WATCH_SECONDS = 600
_ALL_COLUMNS = ("title", "type", "engines", "installed")
_FOLDER_COLUMNS = ("title", "type", "engines")


class SignInRequired(Exception):
    """The saved Epic session ended."""

    def __init__(self) -> None:
        super().__init__("Your Epic session ended. Sign in again.")


def main() -> None:
    _enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


def default_engine(versions: list[str], preferred: str | None) -> str:
    """Use the preferred engine version when the item has it, otherwise the newest."""
    return preferred if preferred in versions else versions[-1]


def login_code_from_clipboard(text: str, ignored: str) -> str | None:
    """The authorization code, if the clipboard holds a new copy of Epic's login JSON page.

    Only the whole JSON page counts, so other text the user copies is never sent to Epic.
    """
    if text == ignored or not text.strip().startswith("{") or "authorizationCode" not in text:
        return None
    try:
        return auth.parse_authorization_code(text)
    except auth.AuthError:
        return None


class App:
    def __init__(
        self,
        root: tk.Tk,
        *,
        http: HttpClient | None = None,
        store: SessionStore | None = None,
        settings_path: Path = SETTINGS_PATH,
        start_thread: Callable[[Callable[[], None]], None] | None = None,
        engines: list[Engine] | None = None,
    ):
        self.root = root
        self.http = http or HttpClient()
        self.store = store or SessionStore.default()
        self.settings_path = settings_path
        self.settings = Settings.load(settings_path)
        self.start_thread = start_thread or (lambda task: threading.Thread(target=task, daemon=True).start())
        self.engines = _with_custom_engines(find_engines() if engines is None else engines, self.settings.custom_engines)
        self.session: Session | None = None
        self.project: Project | None = None
        self.clipboard_job: str | None = None
        self.clipboard_ignored = ""  # The clipboard text when the login page was opened.
        self.clipboard_deadline = 0.0
        self.items: list[LibraryItem] = []
        self.rows: dict[str, LibraryItem] = {}
        self.installed: dict[str, InstalledPlugin] = {}  # By Fab asset id.
        self.updates: dict[str, UpdateStatus] = {}  # By Fab asset id.
        self.engine_item_id: str | None = None  # The item the engine box was filled for.
        self.result_folder: Path | None = None
        self.busy = False
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()

        self._build()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Destroy>", self._destroyed, add="+")
        self._poll_job = root.after(_POLL_MS, self._poll)
        self._open_saved_project()
        self._apply_destination()
        self._run(
            "Checking the saved session...",
            lambda: auth.load_session(self.http, self.store),
            self._session_checked,
            on_error=lambda: self._set_session(None),
        )

    @property
    def destination(self) -> str:
        return self.destination_var.get()

    def selected_engine(self) -> Engine | None:
        """The engine picked for Install to engine."""
        index = self.engine_choice.current()
        return self.engines[index] if 0 <= index < len(self.engines) else None

    def plugin_target(self) -> PluginTarget | None:
        """Where plugins are installed for the current destination, if one is chosen."""
        if self.destination == PROJECT and self.project:
            return PluginTarget.for_project(self.project)
        engine = self.selected_engine()
        if self.destination == ENGINE and engine:
            return PluginTarget.for_engine(engine)
        return None

    # Layout

    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.minsize(720, 600)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        # Treeview rows have a fixed pixel height; size them from the font so high-DPI text fits.
        line_height = tkfont.nametofont("TkDefaultFont", root=root).metrics("linespace")
        ttk.Style(root).configure("Treeview", rowheight=line_height + 6)

        account = ttk.Frame(root, padding=(12, 12, 12, 0))
        account.grid(row=0, column=0, sticky="ew")
        account.columnconfigure(0, weight=1)
        self.account_label = ttk.Label(account, text="Checking the saved session...")
        self.account_label.grid(row=0, column=0, sticky="w")
        self.sign_out_button = ttk.Button(account, text="Sign out", command=self._sign_out)
        self.sign_out_button.grid(row=0, column=1)
        self.sign_out_button.grid_remove()

        self.sign_in_frame = ttk.LabelFrame(root, text="Sign in to Epic", padding=12)
        self.sign_in_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(12, 0))
        self.sign_in_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.sign_in_frame, text="1. Open the Epic login page and sign in. The page then shows JSON text."
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.login_button = ttk.Button(self.sign_in_frame, text="Open Epic login", command=self._open_login)
        self.login_button.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(
            self.sign_in_frame,
            text='2. Copy the whole page (Ctrl+A, Ctrl+C) to sign in, or paste the "authorizationCode" value here:',
        ).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(self.sign_in_frame, textvariable=self.code_var)
        self.code_entry.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.code_entry.bind("<Return>", lambda _: self._sign_in())
        self.sign_in_button = ttk.Button(self.sign_in_frame, text="Sign in", command=self._sign_in)
        self.sign_in_button.grid(row=3, column=1, padx=(6, 0), pady=(6, 0))
        self.sign_in_frame.grid_remove()

        library = ttk.Frame(root, padding=(12, 12, 12, 0))
        library.grid(row=2, column=0, sticky="nsew")
        library.columnconfigure(1, weight=1)
        library.rowconfigure(1, weight=1)
        ttk.Label(library, text="Search").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._show_items())
        ttk.Entry(library, textvariable=self.search_var).grid(row=0, column=1, sticky="ew", padx=6)
        self.refresh_button = ttk.Button(library, text="Refresh", command=self._load_library)
        self.refresh_button.grid(row=0, column=2)
        self.updates_button = ttk.Button(library, text="Check for updates", command=self._check_updates)
        self.updates_button.grid(row=0, column=3, padx=(6, 0))
        self.tree = ttk.Treeview(library, columns=_ALL_COLUMNS, show="headings", selectmode="browse")
        for column, heading, width, stretch in (
            ("title", "Title", 280, True),
            ("type", "Type", 120, False),
            ("engines", "Engine versions", 180, True),
            ("installed", "Installed", 200, True),
        ):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, stretch=stretch)
        self.tree.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._item_selected())
        scrollbar = ttk.Scrollbar(library, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=1, column=4, sticky="ns", pady=(6, 0))
        self.tree.configure(yscrollcommand=scrollbar.set)
        # Shown over the list while the library loads; placed on top of the tree, not in the grid.
        self.loading_overlay = ttk.Frame(library, padding=16)
        ttk.Label(self.loading_overlay, text="Loading your Fab library...").pack()
        self.loading_bar = ttk.Progressbar(self.loading_overlay, mode="indeterminate", length=220)
        self.loading_bar.pack(pady=(8, 0))

        actions = ttk.Frame(root, padding=12)
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(2, weight=1)
        ttk.Label(actions, text="Engine").grid(row=0, column=0, sticky="w")
        self.engine_var = tk.StringVar()
        self.engine_box = ttk.Combobox(actions, textvariable=self.engine_var, state="disabled", width=12)
        self.engine_box.grid(row=0, column=1, columnspan=2, sticky="w", padx=6)
        self.engine_box.bind("<<ComboboxSelected>>", lambda _: self._engine_selected())

        destination = self.settings.destination if self.settings.destination in _ACTION_LABELS else FOLDER
        self.destination_var = tk.StringVar(value=destination)
        ttk.Label(actions, text="Save to").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.folder_radio = ttk.Radiobutton(
            actions, text="Folder", value=FOLDER, variable=self.destination_var, command=self._destination_changed
        )
        self.folder_radio.grid(row=1, column=1, sticky="w", padx=6, pady=(10, 0))
        self.folder_var = tk.StringVar(value=self.settings.download_folder or str(_downloads_folder()))
        self.folder_var.trace_add("write", lambda *_: self._update_controls())
        self.folder_entry = ttk.Entry(actions, textvariable=self.folder_var)
        self.folder_entry.grid(row=1, column=2, sticky="ew", pady=(10, 0))
        self.folder_button = ttk.Button(actions, text="...", width=3, command=self._browse_folder)
        self.folder_button.grid(row=1, column=3, padx=(6, 0), pady=(10, 0))

        self.project_radio = ttk.Radiobutton(
            actions, text="Project", value=PROJECT, variable=self.destination_var, command=self._destination_changed
        )
        self.project_radio.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        self.project_var = tk.StringVar()
        self.project_entry = ttk.Entry(actions, textvariable=self.project_var, state="readonly", cursor="hand2")
        self.project_entry.grid(row=2, column=2, sticky="ew", pady=(6, 0))
        # The path is read-only, so a click opens the picker instead of placing a caret.
        self.project_entry.bind("<Button-1>", lambda _: self._project_field_clicked())
        self.project_button = ttk.Button(actions, text="...", width=3, command=self._choose_project)
        self.project_button.grid(row=2, column=3, padx=(6, 0), pady=(6, 0))
        self.engine_label = ttk.Label(actions, text="")
        self.engine_label.grid(row=3, column=2, columnspan=2, sticky="w", pady=(2, 0))

        self.engine_radio = ttk.Radiobutton(
            actions, text="Engine", value=ENGINE, variable=self.destination_var, command=self._destination_changed
        )
        self.engine_radio.grid(row=4, column=1, sticky="w", padx=6, pady=(6, 0))
        self.engine_choice = ttk.Combobox(
            actions, state="readonly", values=[engine.describe() for engine in self.engines]
        )
        self.engine_choice.grid(row=4, column=2, sticky="ew", pady=(6, 0))
        self.engine_button = ttk.Button(actions, text="...", width=3, command=self._browse_engine)
        self.engine_button.grid(row=4, column=3, padx=(6, 0), pady=(6, 0))
        self.engine_choice.bind("<<ComboboxSelected>>", lambda _: self._engine_dir_selected())
        # Clicking a greyed-out row's field selects that destination.
        self.folder_entry.bind("<Button-1>", lambda _: self._select_destination(FOLDER), add="+")
        self.engine_choice.bind("<Button-1>", lambda _: self._select_destination(ENGINE), add="+")
        saved_engine = next(
            (i for i, engine in enumerate(self.engines) if str(engine.root) == self.settings.engine_dir), 0
        )
        if self.engines:
            self.engine_choice.current(saved_engine)

        self.action_button = ttk.Button(actions, text="Download", command=self._start_action)
        self.action_button.grid(row=5, column=2, columnspan=2, sticky="e", pady=(12, 0))
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self.status_var = tk.StringVar()
        ttk.Label(actions, textvariable=self.status_var, wraplength=580, justify="left").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        self.open_button = ttk.Button(actions, text="Open folder", command=self._open_result)
        self.open_button.grid(row=7, column=3, sticky="ne", pady=(6, 0))
        self.open_button.grid_remove()

    # Background work

    def _run(
        self,
        status: str,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
        *,
        on_error: Callable[[], None] | None = None,
    ) -> None:
        """Run work off the main thread, then call on_success with its result on the main thread."""
        self.busy = True
        self.status_var.set(status)
        self._update_controls()

        def task() -> None:
            try:
                result = work()
            except Exception as error:  # Every failure is reported in the window.
                self.events.put(partial(self._failed, error, on_error))
            else:
                self.events.put(partial(self._succeeded, on_success, result))

        self.start_thread(task)

    def _succeeded(self, on_success: Callable[[Any], None], result: Any) -> None:
        self.busy = False
        on_success(result)
        self._update_controls()

    def _failed(self, error: Exception, on_error: Callable[[], None] | None) -> None:
        self.busy = False
        message = str(error) if isinstance(error, (SignInRequired, *EXPECTED_ERRORS)) else f"{type(error).__name__}: {error}"
        self.status_var.set(f"Error: {message}")
        if isinstance(error, SignInRequired):
            self._set_session(None)
        elif on_error:
            on_error()
        self._update_controls()
        messagebox.showerror(TITLE, message, parent=self.root)

    def process_events(self) -> None:
        while True:
            try:
                callback = self.events.get_nowait()
            except queue.Empty:
                return
            callback()

    def _poll(self) -> None:
        try:
            self.process_events()
        finally:
            self._poll_job = self.root.after(_POLL_MS, self._poll)

    def _destroyed(self, event: tk.Event) -> None:
        # Children's <Destroy> events also reach the root's bindings.
        if event.widget is self.root:
            self.root.after_cancel(self._poll_job)
            self._stop_clipboard_watch()

    def _fresh_session(self) -> Session:
        """Runs on the worker thread: refresh the session if it is about to expire."""
        session = auth.load_session(self.http, self.store)
        if session is None:
            raise SignInRequired()
        return session

    # Session

    def _session_checked(self, session: Session | None) -> None:
        self._set_session(session)
        if session:
            self._load_library()
        else:
            self.status_var.set("Sign in to see your Fab library.")

    def _set_session(self, session: Session | None) -> None:
        self.session = session
        if session:
            self.account_label.configure(text=f"Signed in as {session.display_name or session.account_id}")
            self.sign_out_button.grid()
            self.sign_in_frame.grid_remove()
            self._stop_clipboard_watch()
        else:
            self.account_label.configure(text="Not signed in")
            self.sign_out_button.grid_remove()
            self.sign_in_frame.grid()
            self._show_loading(False)
            self.items = []
            self._show_items()
        self._update_controls()

    def _open_login(self) -> None:
        webbrowser.open(auth.LOGIN_URL)
        self.code_entry.focus_set()
        # Sign in as soon as the user copies the JSON page, instead of making them paste it.
        self.clipboard_ignored = self._clipboard_text()
        self.clipboard_deadline = time.monotonic() + _CLIPBOARD_WATCH_SECONDS
        if self.clipboard_job is None:
            self.clipboard_job = self.root.after(_CLIPBOARD_POLL_MS, self._watch_clipboard)

    def _watch_clipboard(self) -> None:
        self._stop_clipboard_watch()  # Keep a single pending check.
        if self.session is not None or time.monotonic() > self.clipboard_deadline:
            return
        if not self.busy:
            text = self._clipboard_text()
            if login_code_from_clipboard(text, self.clipboard_ignored):
                self.clipboard_ignored = text  # Try each copied page once.
                self.code_var.set(text)
                self._sign_in()
        self.clipboard_job = self.root.after(_CLIPBOARD_POLL_MS, self._watch_clipboard)

    def _stop_clipboard_watch(self) -> None:
        if self.clipboard_job is not None:
            self.root.after_cancel(self.clipboard_job)
            self.clipboard_job = None

    def _clipboard_text(self) -> str:
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            return ""  # Empty, or not text.

    def _sign_in(self) -> None:
        if self.busy:
            return
        code = self.code_var.get()
        self._run("Signing in...", lambda: auth.sign_in(self.http, self.store, code), self._signed_in)

    def _signed_in(self, session: Session) -> None:
        self.code_var.set("")
        self._set_session(session)
        self._load_library()

    def _sign_out(self) -> None:
        self._run("Signing out...", lambda: auth.logout(self.http, self.store), self._signed_out)

    def _signed_out(self, _ended: bool) -> None:
        self._set_session(None)
        self.status_var.set("Signed out.")

    # Destination

    def _destination_changed(self) -> None:
        self.settings.destination = self.destination
        self._save_settings()
        self._apply_destination()
        if self.busy:
            return
        if self.destination == PROJECT and self.project is None:
            self.status_var.set("Choose your .uproject with the ... button next to Project.")
        elif self.destination == ENGINE and not self.engines:
            self.status_var.set("No Unreal Engine installs were found. Choose an engine folder with the ... button.")

    def _apply_destination(self) -> None:
        """Match the columns, update check, button caption and installed plugins to the destination."""
        installs = self.destination != FOLDER
        self.tree.configure(displaycolumns=_ALL_COLUMNS if installs else _FOLDER_COLUMNS)
        if installs:
            self.updates_button.grid()
        else:
            self.updates_button.grid_remove()
        self.action_button.configure(text=_ACTION_LABELS[self.destination])
        self.updates = {}
        self.engine_item_id = None  # The default engine version depends on the destination.
        self._scan_installed()

    def _select_destination(self, destination: str) -> None:
        if not self.busy and self.destination != destination:
            self.destination_var.set(destination)
            self._destination_changed()

    def _project_field_clicked(self) -> str | None:
        if self.busy:
            return None
        self._select_destination(PROJECT)
        self._choose_project()
        return "break"  # Skip the entry's own click handling; the dialog has focus now.

    def _browse_engine(self) -> None:
        current = self.selected_engine()
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Choose an Unreal Engine folder",
            initialdir=str(current.root if current else Path.home()),
        )
        if not chosen:
            return
        try:
            engine = engine_from_folder(Path(chosen))
        except ProjectError as error:
            messagebox.showerror(TITLE, str(error), parent=self.root)
            return
        index = _engine_index(self.engines, engine.root)
        if index is None:
            self.engines.append(engine)
            index = len(self.engines) - 1
            self.engine_choice.configure(values=[e.describe() for e in self.engines])
            self.settings.custom_engines.append(str(engine.root))
        self.engine_choice.current(index)
        self._select_destination(ENGINE)
        self._engine_dir_selected()

    def _engine_dir_selected(self) -> None:
        engine = self.selected_engine()
        if engine:
            self.settings.engine_dir = str(engine.root)
            self._save_settings()
        self._apply_destination()

    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title="Save downloads in", initialdir=self.folder_var.get() or str(_downloads_folder())
        )
        if chosen:
            self.folder_var.set(str(Path(chosen)))
            self.settings.download_folder = self.folder_var.get()
            self._save_settings()

    def _open_saved_project(self) -> None:
        if not self.settings.project:
            return
        try:
            self._set_project(load_project(Path(self.settings.project)))
        except ProjectError as error:
            self.engine_label.configure(text=f"Could not open the last project: {error}")

    def _choose_project(self) -> None:
        current = Path(self.settings.project).parent if self.settings.project else Path.home()
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Choose your Unreal project",
            initialdir=str(current),
            filetypes=[("Unreal project", "*.uproject")],
        )
        if not chosen:
            return
        try:
            project = load_project(Path(chosen))
        except ProjectError as error:
            messagebox.showerror(TITLE, str(error), parent=self.root)
            return
        self._set_project(project)
        self.settings.project = str(project.path)
        self._save_settings()
        self._apply_destination()

    def _set_project(self, project: Project) -> None:
        self.project = project
        self.project_var.set(str(project.path))
        self.engine_label.configure(text=f"Engine: {project.engine.describe()}")

    def _scan_installed(self) -> None:
        target = self.plugin_target()
        plugins = installed_plugins(target.plugins_dir) if target else []
        self.installed = {plugin.record.asset_id: plugin for plugin in plugins}
        self._show_items()

    # Library

    def _load_library(self) -> None:
        self._show_loading(True)
        self._run(
            "Loading your Fab library...",
            lambda: workflow.unreal_library(self.http, self._fresh_session()),
            self._library_loaded,
            on_error=lambda: self._show_loading(False),
        )

    def _show_loading(self, loading: bool) -> None:
        if loading:
            self.loading_overlay.place(in_=self.tree, relx=0.5, rely=0.5, anchor="center")
            self.loading_bar.start(15)
        else:
            self.loading_bar.stop()
            self.loading_overlay.place_forget()

    def _library_loaded(self, items: list[LibraryItem]) -> None:
        self._show_loading(False)
        self.items = sorted(items, key=lambda item: item.title.casefold())
        self._show_items()
        self.status_var.set(f"{len(items)} items with Unreal Engine builds.")

    def _show_items(self) -> None:
        selected = self.selected_item()
        search = self.search_var.get().strip()
        visible = find_items(self.items, search) if search else self.items
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for item in visible:
            values = (item.title, item.distribution_method or "-", ", ".join(item.engine_versions), self._installed_text(item))
            row = self.tree.insert("", "end", values=values)
            self.rows[row] = item
            if item == selected:
                self.tree.selection_set(row)
        self._item_selected()

    def _installed_text(self, item: LibraryItem) -> str:
        plugin = self.installed.get(item.asset_id)
        if plugin is None:
            return ""
        status = self.updates.get(item.asset_id)
        version = plugin.record.engine_version
        return f"{version} - {status.describe()}" if status else version

    def selected_item(self) -> LibraryItem | None:
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def _item_selected(self) -> None:
        item = self.selected_item()
        versions = item.engine_versions if item else []
        self.engine_box.configure(values=versions)
        # Keep the engine picked for this item when the list is redrawn; pick a default for a new item.
        if item is None:
            self.engine_var.set("")
        elif item.asset_id != self.engine_item_id or self.engine_var.get() not in versions:
            self.engine_var.set(default_engine(versions, self._preferred_engine()))
        self.engine_item_id = item.asset_id if item else None
        self._update_controls()

    def _preferred_engine(self) -> str | None:
        """The engine version the destination needs, or for folders the one picked last."""
        engine = self.selected_engine()
        if self.destination == PROJECT and self.project:
            return self.project.engine.version
        if self.destination == ENGINE and engine:
            return engine.version
        return self.settings.engine_version or None

    def _engine_selected(self) -> None:
        if self.destination == FOLDER:
            self.settings.engine_version = self.engine_var.get()
            self._save_settings()

    # Download, install and updates

    def _start_action(self) -> None:
        if self.destination == PROJECT:
            self._add_to_project()
        elif self.destination == ENGINE:
            self._install_to_engine()
        else:
            self._download()

    def _download(self) -> None:
        item, engine, folder = self.selected_item(), self.engine_var.get(), self.folder_var.get().strip()
        version = item.version_for(engine) if item else None
        if self.busy or item is None or version is None or not folder:
            return
        self.settings.download_folder = folder
        self._save_settings()
        out_dir = workflow.output_folder(Path(folder), item, engine)
        if out_dir.exists():
            messagebox.showerror(TITLE, f"{out_dir} already exists. Delete it or choose another folder.", parent=self.root)
            return
        self._prepare_progress()

        def work() -> list[Path]:
            return workflow.download_item(
                self.http, self._fresh_session(), item, version, out_dir, **self._progress_callbacks(item, engine)
            )

        self._run(f"Preparing {item.title} ({engine})...", work, partial(self._download_finished, out_dir))

    def _download_finished(self, out_dir: Path, paths: list[Path]) -> None:
        folders = workflow.folders_with_plugins(paths)
        lines = [f"Saved to {out_dir}", *(f"Plugin folder: {folder}" for folder in folders)]
        self._show_result(folders[0] if len(folders) == 1 else out_dir, lines)

    def _add_to_project(self) -> None:
        item, engine, project = self.selected_item(), self.engine_var.get(), self.project
        version = item.version_for(engine) if item else None
        if self.busy or item is None or version is None or project is None:
            return
        if not self._confirm_engine(item, engine, project.engine.version, project.name):
            return
        self._prepare_progress()

        def work() -> InstallResult | ContentResult:
            return workflow.add_to_project(
                self.http, self._fresh_session(), item, version, engine, project, **self._progress_callbacks(item, engine)
            )

        self._run(f"Preparing {item.title} ({engine})...", work, self._install_finished)

    def _install_to_engine(self) -> None:
        item, version_name, engine = self.selected_item(), self.engine_var.get(), self.selected_engine()
        version = item.version_for(version_name) if item else None
        if self.busy or item is None or version is None or engine is None:
            return
        if not self._confirm_engine(item, version_name, engine.version, f"the engine at {engine.root}"):
            return
        self._prepare_progress()

        def work() -> InstallResult:
            return workflow.install_to_engine(
                self.http, self._fresh_session(), item, version, engine, **self._progress_callbacks(item, version_name)
            )

        self._run(f"Preparing {item.title} ({version_name})...", work, self._install_finished)

    def _confirm_engine(self, item: LibraryItem, build_version: str, target_version: str, target_name: str) -> bool:
        """Ask before installing a build made for another engine version."""
        return build_version == target_version or messagebox.askyesno(
            TITLE,
            f"This is the {build_version} build of {item.title}, but {target_name} uses "
            f"{target_version}. It may not compile. Install it anyway?",
            parent=self.root,
        )

    def _install_finished(self, result: InstallResult | ContentResult) -> None:
        if isinstance(result, ContentResult):
            folder = result.folders[0] if len(result.folders) == 1 else result.project.root / "Content"
        else:
            folder = result.plugin.folder
            record = result.plugin.record
            self.updates[record.asset_id] = UpdateStatus(record, record.artifact_id, record.build_version)
        self._show_result(folder, workflow.result_lines(result))
        self._scan_installed()

    def _check_updates(self) -> None:
        records = [plugin.record for plugin in self.installed.values()]
        items = list(self.items)
        if self.busy or not records:
            return
        self._run(
            f"Checking {len(records)} installed plugins for updates...",
            lambda: workflow.check_updates(self.http, self._fresh_session(), records, items),
            self._updates_checked,
        )

    def _updates_checked(self, statuses: list[UpdateStatus]) -> None:
        self.updates = {status.record.asset_id: status for status in statuses}
        count = sum(status.update_available for status in statuses)
        if count == 0:
            self.status_var.set("Every installed plugin is up to date.")
        else:
            self.status_var.set(f"{count} update{'s' if count > 1 else ''} available. Select the plugin and install it again.")
        self._show_items()

    def _prepare_progress(self) -> None:
        self.open_button.grid_remove()
        self.progress.configure(value=0, maximum=1)

    def _progress_callbacks(self, item: LibraryItem, engine: str) -> dict[str, Callable]:
        """Callbacks for the worker thread that forward progress to the main thread."""
        return {
            "on_manifest": lambda manifest: self.events.put(partial(self._download_started, item, engine, manifest)),
            "report": lambda done, total: self.events.put(partial(self._show_progress, done, total)),
        }

    def _download_started(self, item: LibraryItem, engine: str, manifest: Manifest) -> None:
        size_mb = sum(entry.size for entry in manifest.files) / 2**20
        self.status_var.set(f"Downloading {item.title} ({engine}): {len(manifest.files)} files, {size_mb:.1f} MB")

    def _show_progress(self, done: int, total: int) -> None:
        self.progress.configure(value=done, maximum=total)

    def _show_result(self, folder: Path, lines: list[str]) -> None:
        self.result_folder = folder
        self.status_var.set("\n".join(lines))
        self.progress.configure(value=1, maximum=1)
        self.open_button.grid()

    def _open_result(self) -> None:
        if self.result_folder and self.result_folder.exists():
            os.startfile(self.result_folder)

    # State

    def _save_settings(self) -> None:
        try:
            self.settings.save(self.settings_path)
        except OSError:
            pass  # Remembering choices is a convenience; the current action still works.

    def _update_controls(self) -> None:
        idle = not self.busy
        signed_in = self.session is not None
        destination = self.destination
        has_item = self.selected_item() is not None
        destination_ready = {
            FOLDER: bool(self.folder_var.get().strip()),
            PROJECT: self.project is not None,
            ENGINE: self.selected_engine() is not None,
        }[destination]
        _enable(self.login_button, idle)
        _enable(self.sign_in_button, idle)
        _enable(self.sign_out_button, idle)
        _enable(self.refresh_button, idle and signed_in)
        _enable(self.updates_button, idle and signed_in and bool(self.installed) and bool(self.items))
        for radio in (self.folder_radio, self.project_radio, self.engine_radio):
            _enable(radio, idle)
        _enable(self.folder_entry, idle and destination == FOLDER)
        _enable(self.folder_button, idle and destination == FOLDER)
        _enable(self.project_button, idle and destination == PROJECT)
        _enable(self.engine_choice, idle and destination == ENGINE and bool(self.engines))
        _enable(self.engine_button, idle and destination == ENGINE)
        _enable(
            self.action_button,
            idle and signed_in and has_item and bool(self.engine_var.get()) and destination_ready,
        )
        self.engine_box.configure(state="readonly" if idle and has_item else "disabled")

    def _close(self) -> None:
        if self.busy and not messagebox.askokcancel(TITLE, "A task is still running. Quit anyway?", parent=self.root):
            return
        self.root.destroy()


def _engine_index(engines: list[Engine], root: Path) -> int | None:
    key = os.path.normcase(os.path.normpath(root))
    return next((i for i, e in enumerate(engines) if os.path.normcase(os.path.normpath(e.root)) == key), None)


def _with_custom_engines(engines: list[Engine], folders: list[str]) -> list[Engine]:
    """Add the engine folders the user picked by hand, skipping ones that moved or are already listed."""
    engines = list(engines)
    for folder in folders:
        try:
            engine = engine_from_folder(Path(folder))
        except ProjectError:
            continue
        if _engine_index(engines, engine.root) is None:
            engines.append(engine)
    return engines


def _enable(widget: ttk.Widget, enabled: bool) -> None:
    widget.state(["!disabled"] if enabled else ["disabled"])


def _downloads_folder() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def _enable_dpi_awareness() -> None:
    """Render sharply on high-DPI displays instead of letting Windows scale a blurry bitmap.

    IDLE does the same at startup.
    """
    if sys.platform == "win32":
        try:
            ctypes.OleDLL("shcore").SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except OSError:
            pass


if __name__ == "__main__":
    main()
