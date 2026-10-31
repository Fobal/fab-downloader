# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Download and assemble the files of a build manifest from Epic's CDN."""

from __future__ import annotations

import hashlib
import os
import queue
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from .manifest import ChunkInfo, FileEntry, FormatError, Guid, Manifest, chunk_path, decode_chunk, guid_hex
from .net import REQUEST_ERRORS

STAGING_SUFFIX = ".fabdl-partial"
# Seconds to wait before each new round of attempts on every CDN, so a short network drop is ridden out.
RETRY_DELAYS: Sequence[float] = (2, 5, 15)
RESUME_HINT = "Start the same download again to continue where it stopped."
_POLL_SECONDS = 0.2  # How often the waiting thread checks whether the download was cancelled.


class DownloadError(Exception):
    """The build could not be downloaded or failed verification."""


class DownloadCancelled(DownloadError):
    """The user cancelled the download."""

    def __init__(self) -> None:
        super().__init__("The download was cancelled.")


def check_stop(stop: threading.Event | None) -> None:
    """Raise DownloadCancelled if the user asked to stop."""
    if stop is not None and stop.is_set():
        raise DownloadCancelled()


def download_build(
    manifest: Manifest,
    base_urls: list[str],
    out_dir: Path,
    fetch: Callable[[str], bytes],
    *,
    workers: int = 8,
    report: Callable[[int, int], None] | None = None,
    stop: threading.Event | None = None,
    retry_delays: Sequence[float] | None = None,
) -> list[Path]:
    """Write every file of the manifest under out_dir, which must not exist yet.

    Chunks are downloaded into a staging folder next to out_dir. The files are assembled there
    and moved into place only after every file passes its SHA-1 check. If the download fails or
    the program is stopped, the chunks are kept, and the next download into the same out_dir
    fetches only the missing ones. Setting stop cancels the download and removes the staging
    folder.
    """
    if out_dir.exists():
        raise DownloadError(f"{out_dir} already exists. Choose another output folder.")
    targets = [(entry, safe_relative_path(entry.filename)) for entry in manifest.files]
    missing = {part.guid for entry in manifest.files for part in entry.parts} - manifest.chunks.keys()
    if missing:
        raise DownloadError(f"The manifest references {len(missing)} unknown chunks.")
    check_stop(stop)

    staging = out_dir.with_name(out_dir.name + STAGING_SUFFIX)
    chunk_dir = staging / "chunks"
    files_dir = staging / "files"
    shutil.rmtree(files_dir, ignore_errors=True)  # Half-assembled files from a run that stopped.
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for unfinished in chunk_dir.glob("*.tmp"):
        unfinished.unlink(missing_ok=True)
    files_dir.mkdir()
    keep_chunks = True
    try:
        delays = RETRY_DELAYS if retry_delays is None else retry_delays
        _download_chunks(manifest, base_urls, chunk_dir, fetch, workers, report, stop, delays)
        for entry, relative in targets:
            check_stop(stop)
            try:
                _assemble(entry, files_dir.joinpath(*relative.parts), chunk_dir)
            except DownloadError:
                # One of its chunks may be bad, so fetch them again next time.
                for part in entry.parts:
                    _chunk_file(chunk_dir, part.guid).unlink(missing_ok=True)
                raise
        files_dir.rename(out_dir)
        keep_chunks = False
    except DownloadCancelled:
        keep_chunks = False
        raise
    except (DownloadError, OSError) as error:
        raise DownloadError(f"{str(error).rstrip('.')}. {RESUME_HINT}") from None
    finally:
        if keep_chunks and _has_files(chunk_dir):
            shutil.rmtree(files_dir, ignore_errors=True)
        else:
            shutil.rmtree(staging, ignore_errors=True)
    return [out_dir.joinpath(*relative.parts) for _, relative in targets]


def safe_relative_path(filename: str) -> PurePosixPath:
    """Reject manifest paths that would write outside the output folder."""
    path = PurePosixPath(filename.replace("\\", "/"))
    if not filename or ":" in filename or path.is_absolute() or any(part in ("", "..") for part in path.parts):
        raise DownloadError(f"Refusing unsafe file path in manifest: {filename!r}")
    return path


def _download_chunks(
    manifest: Manifest,
    base_urls: list[str],
    chunk_dir: Path,
    fetch: Callable[[str], bytes],
    workers: int,
    report: Callable[[int, int], None] | None,
    stop: threading.Event | None,
    retry_delays: Sequence[float],
) -> None:
    needed = list(dict.fromkeys(part.guid for entry in manifest.files for part in entry.parts))
    stored = {path.stem for path in chunk_dir.glob("*.bin")}  # Kept from an earlier attempt.
    todo = [guid for guid in needed if guid_hex(guid) not in stored]
    done = len(needed) - len(todo)
    if report and done:
        report(done, len(needed))
    ending = threading.Event()  # Tells the workers to stop retrying.
    finished: queue.Queue[Future] = queue.Queue()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for guid in todo:
            future = pool.submit(
                _download_chunk,
                manifest.chunks[guid],
                manifest.feature_level,
                base_urls,
                fetch,
                _chunk_file(chunk_dir, guid),
                ending,
                retry_delays,
            )
            future.add_done_callback(finished.put)
        for _ in todo:
            _next_finished(finished, stop).result()
            done += 1
            if report:
                report(done, len(needed))
    finally:
        ending.set()
        pool.shutdown(wait=True, cancel_futures=True)


def _next_finished(finished: queue.Queue[Future], stop: threading.Event | None) -> Future:
    """Wait for the next chunk, checking now and then whether the user cancelled."""
    while True:
        check_stop(stop)
        try:
            return finished.get(timeout=_POLL_SECONDS)
        except queue.Empty:
            continue


def _download_chunk(
    chunk: ChunkInfo,
    feature_level: int,
    base_urls: list[str],
    fetch: Callable[[str], bytes],
    target: Path,
    ending: threading.Event,
    retry_delays: Sequence[float],
) -> None:
    path = chunk_path(chunk, feature_level)
    last_error: Exception | None = None
    for delay in (0, *retry_delays):
        if ending.wait(delay):
            return  # The download is ending; the main thread reports why.
        for base_url in base_urls:
            try:
                guid, payload = decode_chunk(fetch(f"{base_url}/{path}"))
            except (*REQUEST_ERRORS, FormatError) as error:
                last_error = error
                continue
            if guid != chunk.guid:
                last_error = DownloadError(f"{path} contains chunk {guid_hex(guid)}")
                continue
            # Written under another name first, so a chunk file is never left half-written.
            unfinished = target.with_suffix(".tmp")
            unfinished.write_bytes(payload)
            os.replace(unfinished, target)
            return
    raise DownloadError(f"Could not download chunk {path}: {last_error}")


def _assemble(entry: FileEntry, target: Path, chunk_dir: Path) -> None:
    @lru_cache(maxsize=8)
    def load(guid: Guid) -> bytes:
        return _chunk_file(chunk_dir, guid).read_bytes()

    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1()
    with open(target, "wb") as output:
        for part in entry.parts:
            data = load(part.guid)
            if part.offset + part.size > len(data):
                raise DownloadError(f"{entry.filename}: a chunk part lies outside its chunk.")
            piece = memoryview(data)[part.offset:part.offset + part.size]
            output.write(piece)
            digest.update(piece)
    if digest.digest() != entry.sha1:
        raise DownloadError(f"{entry.filename} failed its SHA-1 check.")


def _chunk_file(chunk_dir: Path, guid: Guid) -> Path:
    return chunk_dir / f"{guid_hex(guid)}.bin"


def _has_files(folder: Path) -> bool:
    try:
        return any(folder.iterdir())
    except OSError:
        return False
