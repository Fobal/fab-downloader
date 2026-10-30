"""Build manifests and chunks in Epic's formats."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib

from fabdl.manifest import CHUNK_MAGIC, MANIFEST_MAGIC

GUID_A = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
GUID_B = (0xAAAAAAAA, 0xBBBBBBBB, 0xCCCCCCCC, 0xDDDDDDDD)


def fstring(text: str) -> bytes:
    if not text:
        return struct.pack("<i", 0)
    try:
        return struct.pack("<i", len(text) + 1) + text.encode("ascii") + b"\0"
    except UnicodeEncodeError:
        return struct.pack("<i", -(len(text) + 1)) + text.encode("utf-16-le") + b"\0\0"


def _section(body: bytes) -> bytes:
    return struct.pack("<I", 4 + len(body)) + body


def meta(feature_level: int, build_version: str, data_version: int = 2, extra: bytes = b"") -> bytes:
    body = struct.pack("<BIBI", data_version, feature_level, 0, 0)
    body += fstring("AppName") + fstring(build_version) + fstring("") + fstring("")
    body += struct.pack("<I", 1) + fstring("prerequisite")
    body += fstring("") * 3
    if data_version >= 1:
        body += fstring("build-id")
    if data_version >= 2:
        body += fstring("") * 2
    return _section(body + extra)


def chunk_list(chunks: list[tuple[tuple, int, int]], feature_level: int) -> bytes:
    """chunks: (guid, rolling hash, group) tuples."""
    count = len(chunks)
    body = struct.pack("<BI", 3, count)
    body += b"".join(struct.pack("<IIII", *guid) for guid, _, _ in chunks)
    body += b"".join(struct.pack("<Q", chunk_hash) for _, chunk_hash, _ in chunks)
    body += b"\x07" * 20 * count
    body += bytes(group for _, _, group in chunks)
    body += struct.pack("<I", 1 << 20) * count
    body += struct.pack("<q", 1234) * count
    if feature_level >= 22:
        body += struct.pack("<IIII", 1, 2, 3, 4) * count
        body += struct.pack("<I", 0) * count
        body += b"\0" * 16 * count
    return _section(body)


def file_list(files: list[tuple[str, bytes, list[tuple[tuple, int, int]]]], version: int = 2) -> bytes:
    """files: (name, sha1, [(chunk guid, offset, size)]) tuples."""
    count = len(files)
    body = struct.pack("<BI", version, count)
    body += b"".join(fstring(name) for name, _, _ in files)
    body += fstring("") * count
    body += b"".join(sha1 for _, sha1, _ in files)
    body += b"\0" * count
    body += (struct.pack("<I", 1) + fstring("tag")) * count
    for _, _, parts in files:
        body += struct.pack("<I", len(parts))
        for guid, offset, size in parts:
            body += struct.pack("<I", 28) + struct.pack("<IIII", *guid) + struct.pack("<II", offset, size)
    if version >= 1:
        body += struct.pack("<I", 0) * count
        body += fstring("application/octet-stream") * count
    if version >= 2:
        body += b"\0" * 32 * count
    return _section(body)


def custom_fields() -> bytes:
    return _section(struct.pack("<BI", 0, 0))


def binary_manifest(body: bytes, *, compressed: bool = True, stored_as_extra: int = 0, sha1: bytes | None = None) -> bytes:
    data = zlib.compress(body) if compressed else body
    stored_as = (1 if compressed else 0) | stored_as_extra
    header = struct.pack("<IIII", MANIFEST_MAGIC, 41, len(body), len(data))
    header += sha1 if sha1 is not None else hashlib.sha1(body).digest()
    header += struct.pack("<BI", stored_as, 18)
    return header + data


def chunk_file(guid: tuple, payload: bytes, *, compressed: bool = True, encrypted: bool = False) -> bytes:
    data = zlib.compress(payload) if compressed else payload
    stored_as = (1 if compressed else 0) | (2 if encrypted else 0)
    header = struct.pack("<IIII", CHUNK_MAGIC, 3, 66, len(data))
    header += struct.pack("<IIII", *guid) + struct.pack("<Q", 0) + struct.pack("<B", stored_as)
    header += hashlib.sha1(payload).digest() + struct.pack("<B", 2) + struct.pack("<I", len(payload))
    assert len(header) == 66
    return header + data


def blob(value: int, size: int) -> str:
    return "".join(f"{byte:03d}" for byte in value.to_bytes(size, "little"))


def bytes_blob(data: bytes) -> str:
    return "".join(f"{byte:03d}" for byte in data)


def json_manifest(feature_level: int, chunks: list[tuple[tuple, int, int]], files: list[tuple[str, bytes, list]]) -> bytes:
    def guid_text(guid: tuple) -> str:
        return "".join(f"{part:08X}" for part in guid)

    document = {
        "ManifestFileVersion": blob(feature_level, 4),
        "BuildVersionString": "1.0.0",
        "ChunkFilesizeList": {guid_text(g): blob(1234, 8) for g, _, _ in chunks},
        "ChunkHashList": {guid_text(g): blob(h, 8) for g, h, _ in chunks},
        "ChunkShaList": {guid_text(g): "00" * 20 for g, _, _ in chunks},
        "DataGroupList": {guid_text(g): blob(group, 1) for g, _, group in chunks},
        "FileManifestList": [
            {
                "Filename": name,
                "FileHash": bytes_blob(sha1),
                "FileChunkParts": [
                    {"Guid": guid_text(guid), "Offset": blob(offset, 4), "Size": blob(size, 4)}
                    for guid, offset, size in parts
                ],
            }
            for name, sha1, parts in files
        ],
    }
    return json.dumps(document).encode()
