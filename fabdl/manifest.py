# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Parse Epic build manifests (binary and JSON) and chunk files."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from dataclasses import dataclass

MANIFEST_MAGIC = 0x44BEC00C
CHUNK_MAGIC = 0xB1FE3AA2
_COMPRESSED = 0x1
_ENCRYPTED = 0x2

Guid = tuple[int, int, int, int]


class FormatError(Exception):
    """A manifest or chunk could not be read."""


@dataclass(frozen=True)
class ChunkInfo:
    guid: Guid
    hash: int
    group: int
    secret_guid: Guid | None = None


@dataclass(frozen=True)
class ChunkPart:
    guid: Guid
    offset: int
    size: int


@dataclass(frozen=True)
class FileEntry:
    filename: str
    sha1: bytes
    parts: tuple[ChunkPart, ...]

    @property
    def size(self) -> int:
        return sum(part.size for part in self.parts)


@dataclass(frozen=True)
class Manifest:
    feature_level: int
    build_version: str
    chunks: dict[Guid, ChunkInfo]
    files: tuple[FileEntry, ...]


def parse_manifest(data: bytes) -> Manifest:
    try:
        if len(data) >= 4 and struct.unpack_from("<I", data)[0] == MANIFEST_MAGIC:
            return _parse_binary(data)
        return _parse_json(data)
    except (struct.error, UnicodeDecodeError, ValueError, KeyError, TypeError, OverflowError, zlib.error) as error:
        raise FormatError(f"Invalid manifest: {error}") from None


def chunk_path(chunk: ChunkInfo, feature_level: int) -> str:
    """Path of a chunk relative to a CDN base URL."""
    folder = _chunk_folder(feature_level)
    if feature_level >= 22:
        secret = chunk.secret_guid
        secret_part = "plain" if secret is None or secret == (0, 0, 0, 0) else _b64(struct.pack("<IIII", *secret))
        hash_part = _b64(struct.pack("<Q", chunk.hash))
        guid_part = _b64(struct.pack("<IIII", *chunk.guid))
        return f"{folder}/{secret_part}/{chunk.group:02d}/{hash_part}_{guid_part}.chunk"
    return f"{folder}/{chunk.group:02d}/{chunk.hash:016X}_{guid_hex(chunk.guid)}.chunk"


def guid_hex(guid: Guid) -> str:
    return "".join(f"{part:08X}" for part in guid)


def decode_chunk(data: bytes) -> tuple[Guid, bytes]:
    """Return the GUID and uncompressed payload of a downloaded chunk file."""
    reader = _Reader(data)
    if reader.u32() != CHUNK_MAGIC:
        raise FormatError("Invalid chunk: bad magic")
    reader.u32()  # header version
    header_size = reader.u32()
    reader.u32()  # compressed size
    guid = reader.guid()
    reader.read(8)  # rolling hash
    stored_as = reader.u8()
    if stored_as & _ENCRYPTED:
        raise FormatError("Encrypted chunks are not supported")
    if header_size > len(data):
        raise FormatError("Invalid chunk: header is larger than the file")
    payload = data[header_size:]
    if stored_as & _COMPRESSED:
        try:
            payload = zlib.decompress(payload)
        except zlib.error as error:
            raise FormatError(f"Invalid chunk: {error}") from None
    return guid, payload


def _chunk_folder(feature_level: int) -> str:
    if feature_level >= 22:
        return "ChunksV5"
    if feature_level >= 15:
        return "ChunksV4"
    if feature_level >= 6:
        return "ChunksV3"
    if feature_level >= 3:
        return "ChunksV2"
    return "Chunks"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().strip("=")


class _Reader:
    def __init__(self, data: bytes):
        self._data = data
        self.pos = 0

    def read(self, size: int) -> bytes:
        end = self.pos + size
        if size < 0 or end > len(self._data):
            raise FormatError("Unexpected end of data")
        chunk = self._data[self.pos:end]
        self.pos = end
        return chunk

    def seek(self, pos: int) -> None:
        if pos > len(self._data):
            raise FormatError("Unexpected end of data")
        self.pos = pos

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))

    def u8(self) -> int:
        return self.unpack("<B")[0]

    def u32(self) -> int:
        return self.unpack("<I")[0]

    def u64(self) -> int:
        return self.unpack("<Q")[0]

    def guid(self) -> Guid:
        return self.unpack("<IIII")

    def fstring(self) -> str:
        # Positive lengths are ASCII, negative lengths UTF-16 characters; both include a terminator.
        length = self.unpack("<i")[0]
        if length > 0:
            return self.read(length)[:-1].decode("ascii")
        if length < 0:
            return self.read(-length * 2)[:-2].decode("utf-16-le")
        return ""

    def end_section(self, start: int, size: int) -> None:
        """Skip fields newer than this parser knows about."""
        if self.pos > start + size:
            raise FormatError("Section is larger than its declared size")
        self.seek(start + size)


def _parse_binary(data: bytes) -> Manifest:
    header = _Reader(data)
    header.u32()  # magic
    header_size = header.u32()
    header.u32()  # uncompressed size
    header.u32()  # compressed size
    sha1 = header.read(20)
    stored_as = header.u8()
    if stored_as & _ENCRYPTED:
        raise FormatError("Encrypted manifests are not supported")
    if header_size > len(data):
        raise FormatError("Invalid manifest: header is larger than the file")

    body = data[header_size:]
    if stored_as & _COMPRESSED:
        body = zlib.decompress(body)
        if hashlib.sha1(body).digest() != sha1:
            raise FormatError("Manifest hash does not match its contents")

    reader = _Reader(body)
    feature_level, build_version = _read_meta(reader)
    chunks = _read_chunks(reader, feature_level)
    files = _read_files(reader)
    return Manifest(feature_level, build_version, chunks, files)


def _read_meta(reader: _Reader) -> tuple[int, str]:
    start = reader.pos
    size = reader.u32()
    data_version = reader.u8()
    feature_level = reader.u32()
    reader.u8()  # is_file_data
    reader.u32()  # app_id
    reader.fstring()  # app_name
    build_version = reader.fstring()
    reader.fstring()  # launch_exe
    reader.fstring()  # launch_command
    for _ in range(reader.u32()):
        reader.fstring()  # prerequisite ids
    reader.fstring()  # prereq_name
    reader.fstring()  # prereq_path
    reader.fstring()  # prereq_args
    if data_version >= 1:
        reader.fstring()  # build_id
    if data_version >= 2:
        reader.fstring()  # uninstall_action_path
        reader.fstring()  # uninstall_action_args
    reader.end_section(start, size)
    return feature_level, build_version


def _read_chunks(reader: _Reader, feature_level: int) -> dict[Guid, ChunkInfo]:
    # Each field is stored for every chunk before the next field starts.
    start = reader.pos
    size = reader.u32()
    reader.u8()  # version
    count = reader.u32()
    guids = [reader.guid() for _ in range(count)]
    hashes = [reader.u64() for _ in range(count)]
    reader.read(20 * count)  # SHA-1 hashes
    groups = [reader.u8() for _ in range(count)]
    reader.read(4 * count)  # window sizes
    reader.read(8 * count)  # compressed file sizes
    secrets: list[Guid | None] = [None] * count
    if feature_level >= 22:
        secrets = [reader.guid() for _ in range(count)]
        reader.read(4 * count)  # compressed window sizes
        reader.read(16 * count)  # encryption tags
    reader.end_section(start, size)
    return {
        guid: ChunkInfo(guid, chunk_hash, group, secret)
        for guid, chunk_hash, group, secret in zip(guids, hashes, groups, secrets)
    }


def _read_files(reader: _Reader) -> tuple[FileEntry, ...]:
    # Each field is stored for every file before the next field starts.
    start = reader.pos
    size = reader.u32()
    reader.u8()  # version
    count = reader.u32()
    names = [reader.fstring() for _ in range(count)]
    for _ in range(count):
        reader.fstring()  # symlink target
    hashes = [reader.read(20) for _ in range(count)]
    reader.read(count)  # flags
    for _ in range(count):
        for _ in range(reader.u32()):
            reader.fstring()  # install tags
    parts = [_read_file_parts(reader) for _ in range(count)]
    # MD5 hashes, MIME types and SHA-256 hashes follow in newer versions; they are not needed.
    reader.end_section(start, size)
    return tuple(FileEntry(name, sha1, file_parts) for name, sha1, file_parts in zip(names, hashes, parts))


def _read_file_parts(reader: _Reader) -> tuple[ChunkPart, ...]:
    parts = []
    for _ in range(reader.u32()):
        start = reader.pos
        size = reader.u32()
        guid = reader.guid()
        offset = reader.u32()
        length = reader.u32()
        reader.end_section(start, size)
        parts.append(ChunkPart(guid, offset, length))
    return tuple(parts)


def _parse_json(data: bytes) -> Manifest:
    document = json.loads(data.decode("utf-8-sig"))
    feature_level = _blob_to_int(document.get("ManifestFileVersion", "013000000000"))
    hashes = document["ChunkHashList"]
    groups = document["DataGroupList"]
    chunks = {}
    for key in document["ChunkFilesizeList"]:
        guid = _guid_from_json(key)
        chunks[guid] = ChunkInfo(guid, _blob_to_int(hashes[key]), _blob_to_int(groups[key]))
    files = tuple(
        FileEntry(
            entry.get("Filename", ""),
            _blob_to_int(entry["FileHash"]).to_bytes(20, "little"),
            tuple(
                ChunkPart(_guid_from_json(part["Guid"]), _blob_to_int(part["Offset"]), _blob_to_int(part["Size"]))
                for part in entry["FileChunkParts"]
            ),
        )
        for entry in document["FileManifestList"]
    )
    return Manifest(feature_level, document.get("BuildVersionString", ""), chunks, files)


def _blob_to_int(blob: str) -> int:
    """JSON manifests store numbers as little-endian bytes, each written as three decimal digits."""
    value = 0
    for shift, index in enumerate(range(0, len(blob), 3)):
        value += int(blob[index:index + 3]) << (8 * shift)
    return value


def _guid_from_json(text: str) -> Guid:
    return struct.unpack(">IIII", bytes.fromhex(text))
