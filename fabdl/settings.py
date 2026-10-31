# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Choices remembered between runs."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _config_dir() -> Path:
    """%APPDATA% on Windows, ~/Library/Application Support on macOS, otherwise the XDG config
    folder (~/.config by default)."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home())
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    return Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"  # XDG says to ignore relative paths.


APP_DATA = _config_dir() / "fab-downloader"
SETTINGS_PATH = APP_DATA / "settings.json"


@dataclass
class Settings:
    destination: str = "folder"  # "folder", "project" or "engine": where the window saves builds.
    project: str = ""
    engine_dir: str = ""  # Root folder of the engine chosen for Install to engine.
    custom_engines: list[str] = field(default_factory=list)  # Engine folders added with the ... button.
    download_folder: str = ""
    engine_version: str = ""

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> Settings:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        values = {}
        for f in fields(cls):
            value = data.get(f.name)
            expected = type(getattr(defaults, f.name))  # str, or list of str
            if isinstance(value, expected) and (expected is str or all(isinstance(v, str) for v in value)):
                values[f.name] = value
        return cls(**values)

    def save(self, path: Path = SETTINGS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
