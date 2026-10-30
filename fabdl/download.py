"""Download and assemble the files of a build manifest from Epic's CDN."""

from __future__ import annotations

import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Callable

from .manifest import ChunkInfo, FileEntry, FormatError, Guid, Manifest, chunk_path, decode_chunk, guid_hex
from .net import REQUEST_ERRORS

STAGING_SUFFIX = ".fabdl-partial"
_ATTEMPTS = 3


class DownloadError(Exception):
    """The build could not be downloaded or failed verification."""


def download_build(
    manifest: Manifest,
    base_urls: list[str],
    out_dir: Path,
    fetch: Callable[[str], bytes],
    *,
    workers: int = 8,
    report: Callable[[int, int], None] | None = None,
) -> list[Path]:
    """Write every file of the manifest under out_dir, which must not exist yet.

    Files are assembled in a staging folder next to out_dir and moved into place only after
    every file passes its SHA-1 check, so a failed download leaves nothing behind.
    """
    if out_dir.exists():
        raise DownloadError(f"{out_dir} already exists. Choose another output folder.")
    targets = [(entry, safe_relative_path(entry.filename)) for entry in manifest.files]
    missing = {part.guid for entry in manifest.files for part in entry.parts} - manifest.chunks.keys()
    if missing:
        raise DownloadError(f"The manifest references {len(missing)} unknown chunks.")

    staging = out_dir.with_name(out_dir.name + STAGING_SUFFIX)
    if staging.exists():
        shutil.rmtree(staging)
    chunk_dir = staging / "chunks"
    files_dir = staging / "files"
    chunk_dir.mkdir(parents=True)
    files_dir.mkdir()
    try:
        _download_chunks(manifest, base_urls, chunk_dir, fetch, workers, report)
        for entry, relative in targets:
            _assemble(entry, files_dir.joinpath(*relative.parts), chunk_dir)
        files_dir.rename(out_dir)
    finally:
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
) -> None:
    needed = list(dict.fromkeys(part.guid for entry in manifest.files for part in entry.parts))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [
            pool.submit(
                _download_chunk, manifest.chunks[guid], manifest.feature_level, base_urls, fetch, _chunk_file(chunk_dir, guid)
            )
            for guid in needed
        ]
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            if report:
                report(done, len(futures))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _download_chunk(
    chunk: ChunkInfo, feature_level: int, base_urls: list[str], fetch: Callable[[str], bytes], target: Path
) -> None:
    path = chunk_path(chunk, feature_level)
    last_error: Exception | None = None
    for _ in range(_ATTEMPTS):
        for base_url in base_urls:
            try:
                guid, payload = decode_chunk(fetch(f"{base_url}/{path}"))
            except (*REQUEST_ERRORS, FormatError) as error:
                last_error = error
                continue
            if guid != chunk.guid:
                last_error = DownloadError(f"{path} contains chunk {guid_hex(guid)}")
                continue
            target.write_bytes(payload)
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
