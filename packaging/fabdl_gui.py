# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""PyInstaller entry point for the windowed executable, an app bundle on macOS.

PyInstaller runs its entry script as a top-level module, so the package's relative imports
need this absolute import. With FABDL_SMOKE_TEST set, the release build only checks that the
bundled modules, Tcl/Tk and the window icon load, and exits with code 0.
"""

import os
import sys
import tkinter as tk

from fabdl.gui import main, set_window_icon

if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
    # The bootloader points LD_LIBRARY_PATH at the bundled libraries. This process has already
    # loaded them, and programs it starts (the browser, xdg-open) need the user's own value.
    original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is None:
        os.environ.pop("LD_LIBRARY_PATH", None)
    else:
        os.environ["LD_LIBRARY_PATH"] = original

if os.environ.get("FABDL_SMOKE_TEST"):
    root = tk.Tk()
    set_window_icon(root)
    root.destroy()
else:
    main()
