# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Install downloads into a project or engine, and record which Fab build each plugin is."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .project import Engine, Project

RECORD_FILE = ".fabdl.json"


class InstallError(Exception):
    """The download could not be installed."""


@dataclass(frozen=True)
class InstallRecord:
    """Saved in the installed plugin's folder so updates can be checked later."""

    asset_id: str
    namespace: str
    artifact_id: str
    title: str
    engine_version: str
    build_version: str
    platform: str
    installed_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> InstallRecord:
        data = json.loads(text)
        return cls(**{field.name: str(data[field.name]) for field in fields(cls)})


@dataclass(frozen=True)
class PluginTarget:
    """Where plugins are installed: a project's Plugins folder or an engine's Marketplace folder."""

    name: str  # Shown to the user.
    plugins_dir: Path  # New plugins go here.
    search_dir: Path  # Existing copies of a plugin are looked for here.
    backup_dir: Path  # Replaced copies are moved here.
    staging_dir: Path  # Downloads are assembled here, on the same drive, so installing is a rename.
    strip_binaries: bool  # Source-built engines cannot load prebuilt plugin binaries.
    project_file: Path | None  # The .uproject whose plugin list decides enablement; None for engines.

    @classmethod
    def for_project(cls, project: Project) -> PluginTarget:
        return cls(
            name=project.name,
            plugins_dir=project.plugins_dir,
            search_dir=project.plugins_dir,
            backup_dir=project.root / "Saved" / "PluginBackups",
            staging_dir=project.root / "Saved" / "fabdl",
            strip_binaries=not project.engine.installed,
            project_file=project.path,
        )

    @classmethod
    def for_engine(cls, engine: Engine) -> PluginTarget:
        engine_dir = engine.root / "Engine"
        return cls(
            name=f"the {engine.version} engine",
            plugins_dir=engine_dir / "Plugins" / "Marketplace",
            search_dir=engine_dir / "Plugins",
            backup_dir=engine_dir / "Saved" / "PluginBackups",
            staging_dir=engine_dir / "Intermediate" / "fabdl",
            strip_binaries=not engine.installed,
            project_file=None,
        )


@dataclass(frozen=True)
class InstalledPlugin:
    folder: Path
    record: InstallRecord


@dataclass(frozen=True)
class InstallResult:
    target: PluginTarget
    plugin: InstalledPlugin
    backup: Path | None  # Where the replaced copy was moved.
    removed_binaries: bool  # Prebuilt Binaries and Intermediate were dropped for a source-built engine.
    enabled: bool  # Whether the plugin loads without being enabled in Edit > Plugins.


@dataclass(frozen=True)
class ContentResult:
    project: Project
    folders: list[Path]  # Top-level folders of the pack inside the project's Content folder.
    file_count: int
    backup: Path | None  # Where replaced files were moved.
    skipped: int  # Files outside the pack's Content folder, which were not copied.


def plugin_folders(root: Path) -> Iterator[Path]:
    """Folders under root that hold a .uplugin file. Plugins do not nest, so their contents are skipped."""
    for folder, subfolders, files in os.walk(root):
        if any(name.lower().endswith(".uplugin") for name in files):
            subfolders.clear()
            yield Path(folder)


def installed_plugins(plugins_dir: Path) -> list[InstalledPlugin]:
    """The plugins under plugins_dir that this tool installed."""
    plugins = []
    for folder in plugin_folders(plugins_dir):
        try:
            record = InstallRecord.from_json((folder / RECORD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # Not installed by this tool, or the record is damaged.
        plugins.append(InstalledPlugin(folder, record))
    return plugins


def install_plugin(download_dir: Path, target: PluginTarget, record: InstallRecord) -> InstallResult:
    """Move the plugin in download_dir into the target's plugins folder.

    A plugin already installed with the same name is replaced where it is, and the old copy is
    moved to the target's backup folder. For source-built engines, Binaries and Intermediate are
    removed so the build compiles the plugin.
    """
    found = list(plugin_folders(download_dir))
    if not found:
        raise InstallError(f"{record.title} is not a code plugin. Asset packs can only be added to a project.")
    if len(found) > 1:
        raise InstallError(f"{record.title} contains {len(found)} plugins. Use Download and install them by hand.")
    source = found[0]
    descriptors = [path for path in source.iterdir() if path.suffix.lower() == ".uplugin"]
    if len(descriptors) != 1:
        raise InstallError(f"{record.title} has {len(descriptors)} .uplugin files in one folder.")
    name = descriptors[0].stem

    existing = [folder for folder in plugin_folders(target.search_dir) if (folder / f"{name}.uplugin").is_file()]
    if len(existing) > 1:
        listing = ", ".join(str(folder) for folder in existing)
        raise InstallError(f"{target.name} has several copies of {name}: {listing}. Remove all but one.")
    if existing and not _is_within(existing[0], target.plugins_dir):
        # For engines: a built-in plugin with the same name. Replacing it could break the engine.
        raise InstallError(f"{target.name} already has a plugin named {name} at {existing[0]}. fabdl will not replace it.")
    destination = existing[0] if existing else target.plugins_dir / name

    removed_binaries = target.strip_binaries
    if removed_binaries:
        for folder_name in ("Binaries", "Intermediate"):
            shutil.rmtree(source / folder_name, ignore_errors=True)
    (source / RECORD_FILE).write_text(record.to_json(), encoding="utf-8")

    backup = None
    if destination.exists():
        backup = _unused_path(target.backup_dir / f"{destination.name}-{datetime.now():%Y%m%d-%H%M%S}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(destination, backup)
        except OSError as error:
            raise InstallError(
                f"Could not move the installed {name} aside ({error}). Close the Unreal Editor and try again."
            ) from None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, destination)
    except OSError as error:
        if backup:
            os.rename(backup, destination)
        raise InstallError(f"Could not install {name} into {destination}: {error}") from None
    enabled = is_enabled(target, destination / descriptors[0].name)
    return InstallResult(target, InstalledPlugin(destination, record), backup, removed_binaries, enabled)


def is_enabled(target: PluginTarget, descriptor: Path) -> bool:
    """Whether the plugin loads without being enabled by hand, following UE 5.3's PluginManager.

    For a project, an entry in the .uproject's Plugins list decides; otherwise project plugins are
    enabled unless their descriptor sets EnabledByDefault to false. Engine plugins are enabled only
    when EnabledByDefault is true (FPlugin::IsEnabledByDefault).
    """
    enabled_by_default = _read_json(descriptor).get("EnabledByDefault")
    if target.project_file is None:
        return enabled_by_default is True
    name = descriptor.stem.casefold()
    for reference in _read_json(target.project_file).get("Plugins") or ():
        if isinstance(reference, dict) and str(reference.get("Name", "")).casefold() == name:
            return reference.get("Enabled") is True
    return enabled_by_default is not False


def add_content(download_dir: Path, project: Project) -> ContentResult:
    """Merge an asset pack's Content folder into the project's, like the launcher's Add to Project.

    Files the project already has are moved to Saved/ContentBackups first. If a move fails, for
    example because the Unreal Editor has the file open, every change is undone.
    """
    if next(download_dir.rglob("*.uproject"), None):
        raise InstallError("This is a complete project, not an asset pack. Use Download and open it in Unreal.")
    content = _shallowest_folder(download_dir, "content")
    if content is None:
        raise InstallError("The download has no plugin and no Content folder. Use Download instead.")
    files = [path for path in content.rglob("*") if path.is_file()]
    skipped = sum(1 for path in download_dir.rglob("*") if path.is_file()) - len(files)
    relatives = [path.relative_to(content) for path in files]
    content_dir = project.root / "Content"
    backup_dir = _unused_path(project.root / "Saved" / "ContentBackups" / f"{datetime.now():%Y%m%d-%H%M%S}")

    backed_up: list[Path] = []
    placed: list[Path] = []
    try:
        for relative in relatives:
            if (content_dir / relative).exists():
                _move(content_dir / relative, backup_dir / relative)
                backed_up.append(relative)
        for source, relative in zip(files, relatives):
            _move(source, content_dir / relative)
            placed.append(relative)
    except OSError as error:
        for relative in placed:
            _move(content_dir / relative, content / relative)
        for relative in backed_up:
            _move(backup_dir / relative, content_dir / relative)
        raise InstallError(
            f"Could not add the files to {content_dir} ({error}). Nothing was changed. "
            "Close the Unreal Editor and try again."
        ) from None
    folders = sorted({content_dir / relative.parts[0] for relative in relatives})
    return ContentResult(project, folders, len(files), backup_dir if backed_up else None, skipped)


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _shallowest_folder(root: Path, name: str) -> Path | None:
    matches = [Path(folder) for folder, _, _ in os.walk(root) if Path(folder).name.casefold() == name]
    return min(matches, key=lambda path: len(path.parts), default=None)


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _unused_path(path: Path) -> Path:
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}-{number}")
        number += 1
    return candidate
