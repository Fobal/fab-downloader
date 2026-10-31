# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Read an Unreal project and find the engine it uses, the way Unreal's DesktopPlatform module does.

Unreal keeps its records in FPlatformProcess::ApplicationSettingsDir(): %ProgramData%\\Epic on
Windows, ~/Library/Application Support/Epic on macOS and ~/.config/Epic on Linux.

An engine association like "5.3" names a launcher install, listed in
UnrealEngineLauncher/LauncherInstalled.dat there. Any other value, usually a GUID, names an
engine registered under HKCU\\Software\\Epic Games\\Unreal Engine\\Builds on Windows, or in the
[Installations] section of UnrealEngine/Install.ini on macOS and Linux. An empty value means the
project sits inside the engine's folder tree.
"""

from __future__ import annotations

import configparser
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _application_settings_dir() -> Path:
    """Unreal's FPlatformProcess::ApplicationSettingsDir() on this platform."""
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Epic"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Epic"
    return Path.home() / ".config" / "Epic"


LAUNCHER_LIST = _application_settings_dir() / "UnrealEngineLauncher" / "LauncherInstalled.dat"
_BUILDS_KEY = r"Software\Epic Games\Unreal Engine\Builds"
INSTALL_INI = _application_settings_dir() / "UnrealEngine" / "Install.ini"  # Registered engines on macOS and Linux.


class ProjectError(Exception):
    """The project or its engine could not be read."""


@dataclass(frozen=True)
class Engine:
    root: Path
    version: str  # Fab's spelling, for example UE_5.3
    installed: bool  # A binary build (launcher or installed build) rather than one compiled from source.

    def describe(self) -> str:
        kind = "installed build" if self.installed else "source build"
        return f"{self.version} at {self.root} ({kind})"


@dataclass(frozen=True)
class Project:
    path: Path
    engine: Engine

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def plugins_dir(self) -> Path:
        return self.root / "Plugins"


def load_project(
    path: Path,
    *,
    registered_builds: Callable[[], dict[str, str]] | None = None,
    launcher_installs: Callable[[], dict[str, str]] | None = None,
) -> Project:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ProjectError(f"Could not read {path}: {error}") from None
    if not isinstance(data, dict):
        raise ProjectError(f"{path} is not an Unreal project file.")
    association = data.get("EngineAssociation") or ""
    root = find_engine_root(
        association,
        path.parent,
        registered_builds or _registered_builds,
        launcher_installs or _launcher_installs,
    )
    return Project(path.resolve(), read_engine(root))


def find_engine_root(
    association: str,
    project_dir: Path,
    registered_builds: Callable[[], dict[str, str]],
    launcher_installs: Callable[[], dict[str, str]],
) -> Path:
    if not association:
        for folder in project_dir.resolve().parents:
            if (folder / "Engine" / "Build" / "Build.version").is_file():
                return folder
        raise ProjectError("The project names no engine and is not inside an engine folder.")
    # Registry value names are case-insensitive.
    for name, root in registered_builds().items():
        if name.casefold() == association.casefold():
            return Path(root)
    launcher = launcher_installs()
    if association in launcher:
        return Path(launcher[association])
    raise ProjectError(f"The project's engine {association} is not installed or registered on this computer.")


def find_engines(
    *,
    registered_builds: Callable[[], dict[str, str]] | None = None,
    launcher_installs: Callable[[], dict[str, str]] | None = None,
) -> list[Engine]:
    """Every engine registered on this computer or installed by the launcher, newest first."""
    roots = [*(registered_builds or _registered_builds)().values(), *(launcher_installs or _launcher_installs)().values()]
    engines: dict[str, Engine] = {}
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key in engines:
            continue
        try:
            engines[key] = read_engine(Path(root))
        except ProjectError:
            continue  # Stale entry: the engine was moved or deleted.
    return sorted(engines.values(), key=lambda e: (_version_key(e.version), str(e.root)), reverse=True)


def _version_key(version: str) -> tuple[int, int]:
    major, _, minor = version.removeprefix("UE_").partition(".")
    return int(major), int(minor)


def engine_from_folder(folder: Path) -> Engine:
    """Read the engine in a folder the user picked: its root, or the Engine folder inside it."""
    if not (folder / "Engine" / "Build" / "Build.version").is_file() and folder.name.casefold() == "engine":
        folder = folder.parent
    if not (folder / "Engine" / "Build" / "Build.version").is_file():
        raise ProjectError(f"{folder} is not an Unreal Engine folder. Pick the folder that contains Engine.")
    return read_engine(folder)


def read_engine(root: Path) -> Engine:
    version_file = root / "Engine" / "Build" / "Build.version"
    try:
        data = json.loads(version_file.read_text(encoding="utf-8-sig"))
        version = f"UE_{int(data['MajorVersion'])}.{int(data['MinorVersion'])}"
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProjectError(f"Could not read the engine version from {version_file}: {error}") from None
    # Unreal treats an engine with this file as installed (FApp::IsEngineInstalled).
    installed = (root / "Engine" / "Build" / "InstalledBuild.txt").is_file()
    return Engine(root, version, installed)


def _registered_builds() -> dict[str, str]:
    if sys.platform != "win32":
        return _install_ini_builds()
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BUILDS_KEY)
    except OSError:
        return {}
    builds = {}
    with key:
        index = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, index)
            except OSError:
                return builds
            if isinstance(value, str):
                builds[name] = value
            index += 1


def _install_ini_builds() -> dict[str, str]:
    """Engines registered on macOS and Linux, for example {"{1EB8...}": "/home/me/UnrealEngine"}."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # Keep the names' case; lookups compare them case-insensitively anyway.
    try:
        parser.read(INSTALL_INI, encoding="utf-8-sig")
    except (configparser.Error, UnicodeDecodeError):
        return {}
    if not parser.has_section("Installations"):
        return {}
    return {name: root for name, root in parser.items("Installations") if root}


def _launcher_installs() -> dict[str, str]:
    """Launcher engine installs by version, for example {"5.3": "C:/Epic/UE_5.3"}."""
    try:
        data = json.loads(LAUNCHER_LIST.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    installs = {}
    for entry in data.get("InstallationList") or ():
        if not isinstance(entry, dict):
            continue
        app_name = entry.get("AppName") or ""
        location = entry.get("InstallLocation") or ""
        if app_name.startswith("UE_") and location:
            installs[app_name[3:]] = location
    return installs
