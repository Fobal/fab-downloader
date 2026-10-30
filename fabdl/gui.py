"""Window for browsing the Fab library and saving builds to a folder, a project or an engine.

Network work runs on a background thread. Results come back through a queue that the Tk
main loop drains, because Tk widgets may only be touched from the main thread.
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
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

TITLE = "Fab Downloader"
ICON_PATH = Path(__file__).with_name("icon.ico")
ICON_PNG_PATH = Path(__file__).with_name("icon.png")  # For Tk on X11, which cannot read .ico files.
_SPONSOR_URL = "https://github.com/Fobal/fab-downloader#sponsor"
FOLDER, PROJECT, ENGINE = "folder", "project", "engine"
_ACTION_LABELS = {FOLDER: "Download", PROJECT: "Add to project", ENGINE: "Install to engine"}
_PROJECT_HINT = "Choose your .uproject with the ... button next to Project."
_ENGINE_HINT = "No Unreal Engine installs were found. Choose an engine folder with the ... button."
_POLL_MS = 50
_CLIPBOARD_POLL_MS = 500
_CLIPBOARD_WATCH_SECONDS = 600
_ALL_COLUMNS = ("title", "type", "installed")
_FOLDER_COLUMNS = ("title", "type")
_COLUMN_WIDTHS = {"title": 360, "type": 140, "installed": 220}  # Before stretching to fill the list.

# The window's light theme.
_BACKGROUND = "#FFFFFF"
_HEADER = "#F6F8FA"  # Header bar, list headings, striped rows and inactive fields.
_BORDER = "#D0D7DE"
_TEXT = "#1F2328"
_MUTED = "#59636E"
_DISABLED = "#8C959F"
_HOVER = "#EEF1F4"
_ACCENT = "#0969DA"
_ACCENT_HOVER = "#0757BA"
_SELECTED = "#DDF4FF"
_SPONSOR = "#BF3989"


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
        set_window_icon(root)
        scale = root.winfo_fpixels("1i") / 96
        root.minsize(round(760 * scale), round(640 * scale))
        # A set size keeps the window still when the Installed column or the sign-in steps appear;
        # otherwise Tk resizes it to fit whatever its widgets ask for.
        root.geometry(f"{round(900 * scale)}x{round(720 * scale)}")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)
        self.fonts = _apply_theme(root)

        header = ttk.Frame(root, style="Header.TFrame", padding=(16, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.account_label = ttk.Label(header, text="Checking the saved session...", style="Header.TLabel")
        self.account_label.grid(row=0, column=1, sticky="e", padx=(0, 10))
        self.sign_out_button = ttk.Button(header, text="Sign out", command=self._sign_out)
        self.sign_out_button.grid(row=0, column=2)
        self.sign_out_button.grid_remove()
        ttk.Frame(root, style="Border.TFrame", height=1).grid(row=1, column=0, sticky="ew")

        self.sign_in_frame = ttk.LabelFrame(root, text="Sign in to Epic", padding=12)
        self.sign_in_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=(14, 0))
        self.sign_in_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.sign_in_frame, text="1. Open the Epic login page and sign in. The page then shows JSON text."
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.login_button = ttk.Button(
            self.sign_in_frame, text="Open Epic login", style="Accent.TButton", command=self._open_login
        )
        self.login_button.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            self.sign_in_frame,
            text='2. Copy the whole page (Ctrl+A, Ctrl+C) to sign in, or paste the "authorizationCode" value here:',
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(self.sign_in_frame, textvariable=self.code_var)
        self.code_entry.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.code_entry.bind("<Return>", lambda _: self._sign_in())
        self.sign_in_button = ttk.Button(self.sign_in_frame, text="Sign in", command=self._sign_in)
        self.sign_in_button.grid(row=3, column=1, padx=(8, 0), pady=(8, 0))
        self.sign_in_frame.grid_remove()

        library = ttk.Frame(root, padding=(16, 14, 16, 0))
        library.grid(row=3, column=0, sticky="nsew")
        library.columnconfigure(1, weight=1)
        library.rowconfigure(1, weight=1)
        ttk.Label(library, text="Search").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._show_items())
        ttk.Entry(library, textvariable=self.search_var).grid(row=0, column=1, sticky="ew", padx=8)
        self.refresh_button = ttk.Button(library, text="Refresh", command=self._load_library)
        self.refresh_button.grid(row=0, column=2)
        self.updates_button = ttk.Button(library, text="Check for updates", command=self._check_updates)
        self.updates_button.grid(row=0, column=3, padx=(8, 0))
        # The list and its scrollbar share one border.
        listing = ttk.Frame(library, style="Border.TFrame", padding=1)
        listing.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(10, 0))
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=_ALL_COLUMNS, show="headings", selectmode="browse")
        for column, heading, stretch in (("title", "Title", True), ("type", "Type", False), ("installed", "Installed", True)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=_COLUMN_WIDTHS[column], stretch=stretch)
        self.tree.tag_configure("stripe", background=_HEADER)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._item_selected())
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=partial(_set_scrollbar, scrollbar))
        # Shown over the list while the library loads; placed on top of the tree, not in the grid.
        self.loading_overlay = ttk.Frame(library, style="Border.TFrame", padding=1)
        loading = ttk.Frame(self.loading_overlay, padding=16)
        loading.pack()
        ttk.Label(loading, text="Loading your Fab library...", style="Muted.TLabel").pack()
        self.loading_bar = ttk.Progressbar(loading, mode="indeterminate", length=220)
        self.loading_bar.pack(pady=(10, 0))

        actions = ttk.Frame(root, padding=(16, 14, 16, 0))
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(2, weight=1)
        ttk.Label(actions, text="Engine version").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.engine_var = tk.StringVar()
        self.engine_box = ttk.Combobox(actions, textvariable=self.engine_var, state="disabled", width=12)
        self.engine_box.grid(row=0, column=1, columnspan=2, sticky="w")
        self.engine_box.bind("<<ComboboxSelected>>", lambda _: self._engine_selected())

        destination = self.settings.destination if self.settings.destination in _ACTION_LABELS else FOLDER
        self.destination_var = tk.StringVar(value=destination)
        ttk.Label(actions, text="Save to").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(12, 0))
        self.folder_radio = ttk.Radiobutton(
            actions, text="Folder (recommended)", value=FOLDER, variable=self.destination_var, command=self._destination_changed
        )
        self.folder_radio.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(12, 0))
        self.folder_var = tk.StringVar(value=self.settings.download_folder or str(_downloads_folder()))
        self.folder_var.trace_add("write", lambda *_: self._update_controls())
        self.folder_entry = ttk.Entry(actions, textvariable=self.folder_var)
        self.folder_entry.grid(row=1, column=2, sticky="ew", pady=(12, 0))
        self.folder_button = ttk.Button(actions, text="...", width=3, style="Browse.TButton", command=self._browse_folder)
        self.folder_button.grid(row=1, column=3, padx=(8, 0), pady=(12, 0))

        self.project_radio = ttk.Radiobutton(
            actions, text="Project", value=PROJECT, variable=self.destination_var, command=self._destination_changed
        )
        self.project_radio.grid(row=2, column=1, sticky="w", padx=(0, 12), pady=(8, 0))
        self.project_var = tk.StringVar()
        self.project_entry = ttk.Entry(actions, textvariable=self.project_var, state="readonly", cursor="hand2")
        self.project_entry.grid(row=2, column=2, sticky="ew", pady=(8, 0))
        # The path is read-only, so a click opens the picker instead of placing a caret.
        self.project_entry.bind("<Button-1>", lambda _: self._project_field_clicked())
        self.project_button = ttk.Button(actions, text="...", width=3, style="Browse.TButton", command=self._choose_project)
        self.project_button.grid(row=2, column=3, padx=(8, 0), pady=(8, 0))
        # The project's engine; hidden until a project is open.
        self.engine_label = ttk.Label(actions, text="", style="Muted.TLabel")
        self.engine_label.grid(row=3, column=2, columnspan=2, sticky="w", pady=(4, 0))
        self.engine_label.grid_remove()

        self.engine_radio = ttk.Radiobutton(
            actions, text="Engine", value=ENGINE, variable=self.destination_var, command=self._destination_changed
        )
        self.engine_radio.grid(row=4, column=1, sticky="w", padx=(0, 12), pady=(8, 0))
        self.engine_choice = ttk.Combobox(
            actions, state="readonly", values=[engine.describe() for engine in self.engines]
        )
        self.engine_choice.grid(row=4, column=2, sticky="ew", pady=(8, 0))
        self.engine_button = ttk.Button(actions, text="...", width=3, style="Browse.TButton", command=self._browse_engine)
        self.engine_button.grid(row=4, column=3, padx=(8, 0), pady=(8, 0))
        self.engine_choice.bind("<<ComboboxSelected>>", lambda _: self._engine_dir_selected())
        # Clicking a greyed-out row's field selects that destination.
        self.folder_entry.bind("<Button-1>", lambda _: self._select_destination(FOLDER), add="+")
        self.engine_choice.bind("<Button-1>", lambda _: self._select_destination(ENGINE), add="+")
        saved_engine = next(
            (i for i, engine in enumerate(self.engines) if str(engine.root) == self.settings.engine_dir), 0
        )
        if self.engines:
            self.engine_choice.current(saved_engine)

        ttk.Frame(root, style="Border.TFrame", height=1).grid(row=5, column=0, sticky="ew", padx=16, pady=(16, 0))
        status = ttk.Frame(root, padding=(16, 12, 16, 0))
        status.grid(row=6, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.status_var = tk.StringVar()
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel", wraplength=520, justify="left").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        self.open_button = ttk.Button(status, text="Open folder", command=self._open_result)
        self.open_button.grid(row=1, column=1, sticky="ne", padx=(8, 0), pady=(10, 0))
        self.open_button.grid_remove()
        self.action_button = ttk.Button(status, text="Download", style="Accent.TButton", command=self._start_action)
        self.action_button.grid(row=1, column=2, sticky="ne", padx=(8, 0), pady=(10, 0))

        footer = ttk.Frame(root, padding=(16, 14, 16, 8))
        footer.grid(row=7, column=0, sticky="ew")
        self.sponsor_button = ttk.Button(
            footer, text="♥ Support the dev", style="Link.TButton", cursor="hand2", command=self._open_sponsor
        )
        self.sponsor_button.pack(side="right")

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
            self.status_var.set(self._library_status())

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

    def _open_sponsor(self) -> None:
        webbrowser.open(_SPONSOR_URL)

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
        self._show_destination_hint()

    def _show_destination_hint(self) -> None:
        """Say what the destination still needs, or replace a hint that no longer applies."""
        if self.busy:
            return
        if self.destination == PROJECT and self.project is None:
            self.status_var.set(_PROJECT_HINT)
        elif self.destination == ENGINE and not self.engines:
            self.status_var.set(_ENGINE_HINT)
        elif self.status_var.get() in (_PROJECT_HINT, _ENGINE_HINT):
            self.status_var.set(self._library_status())

    def _apply_destination(self) -> None:
        """Match the columns, button caption and installed plugins to the destination."""
        self.tree.configure(displaycolumns=_FOLDER_COLUMNS if self.destination == FOLDER else _ALL_COLUMNS)
        # Start the columns over, or the Installed column lands past the edge of a stretched Title.
        for column, width in _COLUMN_WIDTHS.items():
            self.tree.column(column, width=width)
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
        self._show_destination_hint()

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
            self.engine_label.grid()

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
        self._show_destination_hint()

    def _set_project(self, project: Project) -> None:
        self.project = project
        self.project_var.set(str(project.path))
        self.engine_label.configure(text=f"Engine: {project.engine.describe()}")
        self.engine_label.grid()

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
        self.status_var.set(self._library_status())

    def _library_status(self) -> str:
        if self.session is None:
            return "Sign in to see your Fab library."
        return f"{len(self.items)} items with Unreal Engine builds."

    def _show_items(self) -> None:
        selected = self.selected_item()
        search = self.search_var.get().strip()
        visible = find_items(self.items, search) if search else self.items
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for index, item in enumerate(visible):
            values = (item.title, _type_text(item), self._installed_text(item))
            row = self.tree.insert("", "end", values=values, tags=("stripe",) if index % 2 else ())
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
            try:
                _open_folder(self.result_folder)
            except OSError as error:
                messagebox.showerror(TITLE, f"Could not open {self.result_folder}: {error}", parent=self.root)

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
        # Only plugins this tool installed can be checked, so the button waits until there is one.
        if self.installed:
            self.updates_button.grid()
        else:
            self.updates_button.grid_remove()
        _enable(self.updates_button, idle and signed_in and bool(self.items))
        for radio in (self.folder_radio, self.project_radio, self.engine_radio):
            _enable(radio, idle)
        _enable(self.folder_entry, idle and destination == FOLDER)
        _enable(self.folder_button, idle and destination == FOLDER)
        _enable(self.project_entry, idle and destination == PROJECT)
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


def _set_scrollbar(scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
    """Show the scrollbar only while the list is longer than it can show."""
    if float(first) <= 0 and float(last) >= 1:
        scrollbar.grid_remove()
    else:
        scrollbar.grid()
    scrollbar.set(first, last)


def _type_text(item: LibraryItem) -> str:
    """CODE_PLUGIN reads as Code plugin."""
    return item.distribution_method.replace("_", " ").capitalize() or "-"


def _apply_theme(root: tk.Tk) -> list[tkfont.Font]:
    """Style the ttk widgets as a flat light theme, and return the fonts it created.

    Built on clam, the one standard theme whose colors can all be changed. Sizes are given in
    pixels at 96 DPI and scaled, because clam draws its arrows and indicators in pixels.
    """
    scale = float(root.tk.call("tk", "scaling")) / (96 / 72)

    def px(size: float) -> int:
        return round(size * scale)

    default_font = tkfont.nametofont("TkDefaultFont", root=root)
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
        tkfont.nametofont(name, root=root).configure(size=10)
    title_font = default_font.copy()
    title_font.configure(size=13, weight="bold")
    bold_font = default_font.copy()
    bold_font.configure(weight="bold")
    link_font = default_font.copy()
    link_font.configure(size=12)

    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=_BACKGROUND)
    style.configure(
        ".",
        background=_BACKGROUND,
        foreground=_TEXT,
        bordercolor=_BORDER,
        lightcolor=_BACKGROUND,
        darkcolor=_BACKGROUND,
        troughcolor=_HEADER,
        focuscolor=_ACCENT,
        selectbackground=_ACCENT,
        selectforeground="#FFFFFF",
        fieldbackground=_BACKGROUND,
        insertcolor=_TEXT,
        arrowcolor=_MUTED,
        font=default_font,
    )
    style.map(".", foreground=[("disabled", _DISABLED)])

    style.configure("Header.TFrame", background=_HEADER)
    style.configure("Border.TFrame", background=_BORDER)  # Shows as a border around a padded child.
    style.configure("Header.TLabel", background=_HEADER, foreground=_MUTED)
    style.configure("Title.TLabel", background=_HEADER, font=title_font)
    style.configure("Section.TLabel", font=bold_font)
    style.configure("Muted.TLabel", foreground=_MUTED)
    style.configure("TLabelframe", bordercolor=_BORDER, lightcolor=_BORDER, darkcolor=_BORDER)
    style.configure("TLabelframe.Label", font=bold_font)

    style.configure("TButton", background=_BACKGROUND, padding=(px(12), px(4)), anchor="center")
    style.configure("Browse.TButton", padding=(px(4), px(4)))
    style.map(
        "TButton",
        background=[("disabled", _BACKGROUND), ("pressed", _BORDER), ("active", _HOVER)],
        lightcolor=[("disabled", _BACKGROUND), ("pressed", _BORDER), ("active", _HOVER)],
        darkcolor=[("disabled", _BACKGROUND), ("pressed", _BORDER), ("active", _HOVER)],
        bordercolor=[("focus", _ACCENT)],
    )
    style.configure(
        "Accent.TButton", background=_ACCENT, foreground="#FFFFFF", bordercolor=_ACCENT,
        lightcolor=_ACCENT, darkcolor=_ACCENT, font=bold_font, padding=(px(20), px(5)),
    )
    accent = [("disabled", _HEADER), ("pressed", _ACCENT_HOVER), ("active", _ACCENT_HOVER)]
    style.map(
        "Accent.TButton",
        background=accent,
        lightcolor=accent,
        darkcolor=accent,
        bordercolor=[("disabled", _BORDER), ("focus", _ACCENT_HOVER)],
        foreground=[("disabled", _DISABLED)],
    )
    # A button drawn as a link: only its focus ring and label, no border.
    style.layout(
        "Link.TButton",
        [("Button.focus", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})],
    )
    style.configure("Link.TButton", foreground=_SPONSOR, font=link_font, padding=0)

    field = {"fieldbackground": [("disabled", _HEADER), ("readonly", _BACKGROUND)],
             "bordercolor": [("focus", _ACCENT)],
             "lightcolor": [("focus", _ACCENT)]}
    style.configure("TEntry", padding=(px(6), px(4)))
    style.map("TEntry", **field)
    style.configure("TCombobox", padding=(px(6), px(4)), background=_BACKGROUND, arrowsize=px(14))
    style.map(
        "TCombobox",
        **field,
        background=[("disabled", _HEADER), ("active", _HOVER)],
        # A readonly combobox highlights its text when focused; keep it plain.
        selectbackground=[("readonly", _BACKGROUND)],
        selectforeground=[("readonly", _TEXT)],
    )
    root.option_add("*TCombobox*Listbox.background", _BACKGROUND)
    root.option_add("*TCombobox*Listbox.foreground", _TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", _SELECTED)
    root.option_add("*TCombobox*Listbox.selectForeground", _TEXT)
    root.option_add("*TCombobox*Listbox.font", default_font)

    style.configure(
        "TRadiobutton", indicatorsize=px(14), indicatormargin=(0, 0, px(6), 0),
        indicatorbackground=_BACKGROUND, indicatorforeground=_ACCENT,
        upperbordercolor=_DISABLED, lowerbordercolor=_DISABLED,
    )
    style.map(
        "TRadiobutton",
        background=[("active", _BACKGROUND)],
        indicatorbackground=[("disabled", _HEADER), ("pressed", _HOVER)],
        indicatorforeground=[("disabled", _DISABLED)],
        upperbordercolor=[("selected", "!disabled", _ACCENT)],
        lowerbordercolor=[("selected", "!disabled", _ACCENT)],
    )

    # Treeview rows have a fixed pixel height; size them from the font so high-DPI text fits.
    style.configure("Treeview", rowheight=default_font.metrics("linespace") + px(10), borderwidth=0)
    style.map("Treeview", background=[("selected", _SELECTED)], foreground=[("selected", _TEXT)])
    style.configure(
        "Treeview.Heading", background=_HEADER, foreground=_MUTED, font=bold_font,
        bordercolor=_BORDER, lightcolor=_HEADER, darkcolor=_HEADER, padding=(px(6), px(5)), relief="flat",
    )
    style.map("Treeview.Heading", background=[("active", _HOVER)])

    # A slim scrollbar without arrow buttons.
    style.layout(
        "Vertical.TScrollbar",
        [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
        ]})],
    )
    style.configure(
        "Vertical.TScrollbar", troughcolor=_BACKGROUND, background=_BORDER, bordercolor=_BACKGROUND,
        lightcolor=_BORDER, darkcolor=_BORDER, arrowsize=px(10), gripcount=0,
    )
    style.map("Vertical.TScrollbar", background=[("active", _DISABLED)])

    style.configure(
        "Horizontal.TProgressbar", background=_ACCENT, troughcolor=_HEADER, bordercolor=_HEADER,
        lightcolor=_ACCENT, darkcolor=_ACCENT, arrowsize=px(6),  # Clam draws the bar as a scrollbar thumb.
    )
    # Keep references: Tk deletes a named font once its Python object is collected.
    return [title_font, bold_font, link_font]


def _enable(widget: ttk.Widget, enabled: bool) -> None:
    widget.state(["!disabled"] if enabled else ["disabled"])


def set_window_icon(root: tk.Tk) -> None:
    """Give the window and the dialogs it opens the app icon."""
    if sys.platform == "win32":
        root.iconbitmap(default=str(ICON_PATH))
    else:
        # Tk copies the image into the icon, so the PhotoImage need not be kept.
        root.iconphoto(True, tk.PhotoImage(master=root, file=str(ICON_PNG_PATH)))


def _open_folder(folder: Path) -> None:
    if sys.platform == "win32":
        os.startfile(folder)
    else:
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
