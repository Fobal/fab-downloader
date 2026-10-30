"""PyInstaller entry point for fab-downloader.exe (windowed).

PyInstaller runs its entry script as a top-level module, so the package's relative imports
need this absolute import. With FABDL_SMOKE_TEST set, the release build only checks that the
bundled modules and Tcl/Tk load, and exits with code 0.
"""

import os
import tkinter as tk

from fabdl.gui import main

if os.environ.get("FABDL_SMOKE_TEST"):
    tk.Tk().destroy()
else:
    main()
