"""PyInstaller entry point for fab-downloader.exe (windowed).

PyInstaller runs its entry script as a top-level module, so the package's relative imports
need this absolute import. With FABDL_SMOKE_TEST set, the release build only checks that the
bundled modules, Tcl/Tk and the window icon load, and exits with code 0.
"""

import os
import tkinter as tk

from fabdl.gui import ICON_PATH, main

if os.environ.get("FABDL_SMOKE_TEST"):
    root = tk.Tk()
    root.iconbitmap(default=str(ICON_PATH))
    root.destroy()
else:
    main()
