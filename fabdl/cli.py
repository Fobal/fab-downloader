"""Command-line interface: fabdl list | download | install | outdated | logout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from . import auth, workflow
from .auth import Session, SessionStore
from .fab import LibraryItem, find_items, normalize_engine_version
from .install import PluginTarget, installed_plugins
from .manifest import Manifest
from .net import HttpClient
from .project import Engine, Project, engine_from_folder, load_project
from .settings import Settings
from .workflow import EXPECTED_ERRORS

T = TypeVar("T")


class UsageError(Exception):
    """The request cannot be carried out as given."""


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (UsageError, *EXPECTED_ERRORS) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fabdl", description="Download Unreal Engine builds of items in your Fab library."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_command = commands.add_parser("list", help="list the Unreal Engine items in your Fab library")
    list_command.add_argument("search", nargs="?", help="only show titles containing this text")
    list_command.set_defaults(handler=_list)

    download = commands.add_parser("download", help="download one engine version of an item")
    download.add_argument("search", nargs="?", help="asset id, or text from the title")
    download.add_argument("--engine", help="engine version, for example 5.3")
    download.add_argument("--platform", default="Windows", help="build platform (default: Windows)")
    download.add_argument("--out", type=Path, help="output folder; must not exist yet")
    download.set_defaults(handler=_download)

    install = commands.add_parser(
        "install", help="add an item to a project (plugin or asset pack), or install a plugin into an engine"
    )
    install.add_argument("search", nargs="?", help="asset id, or text from the title")
    _add_target_arguments(install)
    install.add_argument("--engine", help="engine version (default: the target's engine)")
    install.add_argument("--platform", default="Windows", help="build platform (default: Windows)")
    install.set_defaults(handler=_install)

    outdated = commands.add_parser("outdated", help="check the plugins installed by fabdl for updates")
    _add_target_arguments(outdated)
    outdated.set_defaults(handler=_outdated)

    logout = commands.add_parser("logout", help="end and forget the saved Epic session")
    logout.set_defaults(handler=_logout)
    return parser


def _list(args: argparse.Namespace) -> None:
    _, items = _unreal_library(HttpClient())
    if args.search:
        items = find_items(items, args.search)
    for item in sorted(items, key=lambda i: i.title.casefold()):
        print(f"{item.title}  [{item.distribution_method or '-'}]")
        print(f"    id: {item.asset_id}    engines: {', '.join(item.engine_versions)}")
    print(f"{len(items)} items")


def _download(args: argparse.Namespace) -> None:
    engine = _engine_version(args.engine) if args.engine else None
    http = HttpClient()
    session, items = _unreal_library(http)
    item = _choose_item(items, args.search, interactive=sys.stdin.isatty())
    engine = engine or _choose_engine(item, interactive=sys.stdin.isatty())
    version = item.version_for(engine)
    if version is None:
        raise UsageError(f"{item.title} has no {engine} build. Available: {', '.join(item.engine_versions)}")

    out_dir = args.out or workflow.output_folder(Path.cwd(), item, engine)
    if out_dir.exists():
        raise UsageError(f"{out_dir} already exists. Pass --out with a new folder.")

    def announce(manifest: Manifest) -> None:
        size_mb = sum(entry.size for entry in manifest.files) / 2**20
        print(f"Downloading {item.title} ({engine}, build {manifest.build_version or 'unknown'}): "
              f"{len(manifest.files)} files, {size_mb:.1f} MB")

    try:
        paths = workflow.download_item(
            http, session, item, version, out_dir,
            platform=args.platform, on_manifest=announce, report=_report_progress,
        )
    finally:
        print()  # End the progress line.
    print(f"Saved to {out_dir}")
    for folder in workflow.folders_with_plugins(paths):
        print(f"Plugin folder: {folder}")


def _install(args: argparse.Namespace) -> None:
    project, engine_target = _target(args)
    target_engine = engine_target or project.engine
    engine = _engine_version(args.engine) if args.engine else target_engine.version
    http = HttpClient()
    session, items = _unreal_library(http)
    item = _choose_item(items, args.search, interactive=sys.stdin.isatty())
    version = item.version_for(engine)
    if version is None:
        raise UsageError(
            f"{item.title} has no {engine} build. Available: {', '.join(item.engine_versions)}. "
            "Pass --engine to install another version."
        )
    where = project.name if project else "the engine"
    print(f"Installing {item.title} ({engine}) into {where}, {target_engine.describe()}")
    try:
        if project:
            result = workflow.add_to_project(
                http, session, item, version, engine, project, platform=args.platform, report=_report_progress
            )
        else:
            result = workflow.install_to_engine(
                http, session, item, version, engine_target, platform=args.platform, report=_report_progress
            )
    finally:
        print()  # End the progress line.
    for line in workflow.result_lines(result):
        print(line)


def _outdated(args: argparse.Namespace) -> None:
    project, engine = _target(args)
    target = PluginTarget.for_project(project) if project else PluginTarget.for_engine(engine)
    plugins = installed_plugins(target.plugins_dir)
    if not plugins:
        print(f"{target.name[0].upper()}{target.name[1:]} has no plugins installed by fabdl.")
        return
    http = HttpClient()
    session, items = _unreal_library(http)
    for status in workflow.check_updates(http, session, [plugin.record for plugin in plugins], items):
        print(f"{status.record.title} ({status.record.engine_version}): {status.describe()}")


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project", type=Path, help="the .uproject file (default: the project last used)")
    group.add_argument("--engine-dir", type=Path, help="an engine's root folder, to install into the engine itself")


def _target(args: argparse.Namespace) -> tuple[Project | None, Engine | None]:
    """The project, or with --engine-dir the engine, that the command works on."""
    if args.engine_dir:
        return None, engine_from_folder(args.engine_dir)
    return _project(args.project), None


def _project(path: Path | None) -> Project:
    """Load the given project, or the one used last."""
    if path is None:
        saved = Settings.load().project
        if not saved:
            raise UsageError("Pass --project with the path of your .uproject file, or --engine-dir.")
        path = Path(saved)
    return load_project(path)


def _logout(args: argparse.Namespace) -> None:
    ended = auth.logout(HttpClient(), SessionStore.default())
    print("Signed out." if ended else "No saved session.")


def _unreal_library(http: HttpClient) -> tuple[Session, list[LibraryItem]]:
    session = auth.get_session(http, SessionStore.default())
    return session, workflow.unreal_library(http, session)


def _engine_version(text: str) -> str:
    try:
        return normalize_engine_version(text)
    except ValueError as error:
        raise UsageError(str(error)) from None


def _choose_item(items: list[LibraryItem], search: str | None, *, interactive: bool) -> LibraryItem:
    matches = find_items(items, search) if search else items
    if not matches:
        raise UsageError(f"No Unreal Engine item in your Fab library matches {search!r}.")
    if len(matches) == 1:
        return matches[0]
    matches = sorted(matches, key=lambda i: i.title.casefold())
    if not interactive:
        listing = "\n".join(f"  {item.title} (id {item.asset_id})" for item in matches)
        raise UsageError(f"{len(matches)} items match. Search for a more specific title or an id:\n{listing}")
    return _pick("Item", matches, lambda i: f"{i.title}  ({', '.join(i.engine_versions)})")


def _choose_engine(item: LibraryItem, *, interactive: bool) -> str:
    versions = item.engine_versions
    if len(versions) == 1:
        return versions[0]
    if not interactive:
        raise UsageError(f"Pass --engine. {item.title} is available for {', '.join(versions)}.")
    return _pick("Engine version", versions, str)


def _pick(label: str, options: list[T], describe: Callable[[T], str]) -> T:
    for number, option in enumerate(options, 1):
        print(f"{number:3}. {describe(option)}")
    while True:
        answer = input(f"{label} number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print(f"Enter a number from 1 to {len(options)}.")


def _report_progress(done: int, total: int) -> None:
    print(f"\r  chunks {done}/{total}", end="", flush=True)
