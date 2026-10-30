"""Choices remembered between runs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DATA = Path(os.environ.get("APPDATA") or Path.home()) / "fab-downloader"
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
