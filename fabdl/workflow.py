"""Steps shared by the command line and the window."""

from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .auth import AuthError, Session
from .download import DownloadError, download_build
from .fab import DownloadSource, FabError, LibraryItem, ProjectVersion, list_library, request_build
from .install import (
    ContentResult,
    InstallError,
    InstallRecord,
    InstallResult,
    PluginTarget,
    add_content,
    install_plugin,
    plugin_folders,
)
from .manifest import FormatError, Manifest, parse_manifest
from .net import REQUEST_ERRORS, HttpClient
from .project import Engine, Project, ProjectError

# Failures shown to the user as a plain message; anything else is a bug.
EXPECTED_ERRORS = (AuthError, FabError, FormatError, DownloadError, InstallError, ProjectError, *REQUEST_ERRORS)


@dataclass(frozen=True)
class UpdateStatus:
    record: InstallRecord
    latest_artifact_id: str | None  # None when the library no longer offers the item for that engine.
    latest_build_version: str | None

    @property
    def available(self) -> bool:
        return self.latest_artifact_id is not None

    @property
    def update_available(self) -> bool:
        return self.available and (
            self.latest_artifact_id != self.record.artifact_id
            or self.latest_build_version != self.record.build_version
        )

    def describe(self) -> str:
        if not self.available:
            return f"no longer in your library for {self.record.engine_version}"
        return "update available" if self.update_available else "up to date"


def unreal_library(http: HttpClient, session: Session) -> list[LibraryItem]:
    """The library items that have Unreal Engine builds."""
    return [item for item in list_library(http, session) if item.project_versions]


def output_folder(parent: Path, item: LibraryItem, engine_version: str) -> Path:
    return parent / safe_folder_name(f"{item.title} {engine_version}")


def safe_folder_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .") or "fab-download"


def download_item(
    http: HttpClient,
    session: Session,
    item: LibraryItem,
    version: ProjectVersion,
    out_dir: Path,
    *,
    platform: str = "Windows",
    on_manifest: Callable[[Manifest], None] | None = None,
    report: Callable[[int, int], None] | None = None,
) -> list[Path]:
    """Download one engine version of an item into out_dir, which must not exist yet."""
    paths, _ = _download(http, session, item, version, out_dir, platform, on_manifest, report)
    return paths


def add_to_project(
    http: HttpClient,
    session: Session,
    item: LibraryItem,
    version: ProjectVersion,
    engine_version: str,
    project: Project,
    *,
    platform: str = "Windows",
    on_manifest: Callable[[Manifest], None] | None = None,
    report: Callable[[int, int], None] | None = None,
) -> InstallResult | ContentResult:
    """Install a plugin into the project's Plugins folder, or merge an asset pack into its Content."""
    target = PluginTarget.for_project(project)
    with _work_dir(target) as work_dir:
        record = _download_for_install(http, session, item, version, engine_version, work_dir, platform, on_manifest, report)
        if any(plugin_folders(work_dir)):
            return install_plugin(work_dir, target, record)
        return add_content(work_dir, project)


def install_to_engine(
    http: HttpClient,
    session: Session,
    item: LibraryItem,
    version: ProjectVersion,
    engine: Engine,
    *,
    platform: str = "Windows",
    on_manifest: Callable[[Manifest], None] | None = None,
    report: Callable[[int, int], None] | None = None,
) -> InstallResult:
    """Install a plugin into the engine's Plugins/Marketplace folder, like the launcher's Install to Engine."""
    target = PluginTarget.for_engine(engine)
    with _work_dir(target) as work_dir:
        record = _download_for_install(http, session, item, version, engine.version, work_dir, platform, on_manifest, report)
        return install_plugin(work_dir, target, record)


def result_lines(result: InstallResult | ContentResult) -> list[str]:
    """What an install did, for the window and the command line."""
    if isinstance(result, ContentResult):
        names = ", ".join(folder.name for folder in result.folders)
        lines = [f"Added {_count(result.file_count, 'file')} to {result.project.root / 'Content'} ({names})"]
        if result.backup:
            lines.append(f"Files it replaced were moved to {result.backup}")
        if result.skipped:
            lines.append(f"Skipped {_count(result.skipped, 'file')} outside the pack's Content folder.")
        return lines
    record = result.plugin.record
    lines = [f"Installed {record.title} ({record.engine_version}) into {result.plugin.folder}"]
    if result.backup:
        lines.append(f"The previous version was moved to {result.backup}")
    if result.removed_binaries:
        lines.append("Build the project to compile the plugin.")
    if not result.enabled:
        if result.target.project_file is None:
            lines.append("The plugin is off by default. Enable it in each project's Edit > Plugins.")
        else:
            lines.append(f"The plugin is disabled in {result.target.name}. Enable it in Edit > Plugins.")
    return lines


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _download_for_install(
    http: HttpClient,
    session: Session,
    item: LibraryItem,
    version: ProjectVersion,
    engine_version: str,
    work_dir: Path,
    platform: str,
    on_manifest: Callable[[Manifest], None] | None,
    report: Callable[[int, int], None] | None,
) -> InstallRecord:
    _, build_version = _download(http, session, item, version, work_dir, platform, on_manifest, report)
    return InstallRecord(
        asset_id=item.asset_id,
        namespace=item.namespace,
        artifact_id=version.artifact_id,
        title=item.title,
        engine_version=engine_version,
        build_version=build_version,
        platform=platform,
        installed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


@contextmanager
def _work_dir(target: PluginTarget) -> Iterator[Path]:
    """A fresh download folder on the target's drive, removed afterwards."""
    work_dir = target.staging_dir / f"download-{uuid.uuid4().hex[:8]}"
    try:
        yield work_dir
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            work_dir.parent.rmdir()
        except OSError:
            pass  # Another download is using it, or it was never created.


def check_updates(
    http: HttpClient, session: Session, records: list[InstallRecord], items: list[LibraryItem]
) -> list[UpdateStatus]:
    """Ask Fab for the current build of each installed plugin. One request per plugin."""
    by_id = {item.asset_id: item for item in items}
    statuses = []
    for record in records:
        item = by_id.get(record.asset_id)
        version = item.version_for(record.engine_version) if item else None
        if item is None or version is None:
            statuses.append(UpdateStatus(record, None, None))
            continue
        build = request_build(http, session, item, version, record.platform)
        statuses.append(UpdateStatus(record, version.artifact_id, build.build_version))
    return statuses


def fetch_manifest(http: HttpClient, sources: list[DownloadSource]) -> tuple[Manifest, list[DownloadSource]]:
    """Download the manifest from the first CDN that serves it; that CDN is tried first for chunks."""
    last_error: Exception | None = None
    for index, source in enumerate(sources):
        try:
            manifest = parse_manifest(http.request("GET", source.manifest_url))
        except (*REQUEST_ERRORS, FormatError) as error:
            last_error = error
            continue
        return manifest, [source, *sources[:index], *sources[index + 1:]]
    raise DownloadError(f"Could not download the build manifest: {last_error}")


def folders_with_plugins(paths: list[Path]) -> list[Path]:
    """Folders that hold a .uplugin file."""
    return [path.parent for path in paths if path.suffix.lower() == ".uplugin"]


def _download(
    http: HttpClient,
    session: Session,
    item: LibraryItem,
    version: ProjectVersion,
    out_dir: Path,
    platform: str,
    on_manifest: Callable[[Manifest], None] | None,
    report: Callable[[int, int], None] | None,
) -> tuple[list[Path], str]:
    """Returns the downloaded paths and Fab's build version, which update checks compare against."""
    build = request_build(http, session, item, version, platform)
    manifest, sources = fetch_manifest(http, build.sources)
    if on_manifest:
        on_manifest(manifest)
    paths = download_build(
        manifest,
        [source.base_url for source in sources],
        out_dir,
        lambda url: http.request("GET", url),
        report=report,
    )
    return paths, build.build_version
