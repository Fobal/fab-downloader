# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import unittest

from fabdl.manifest import ChunkInfo, ChunkPart, FormatError, chunk_path, decode_chunk, parse_manifest

from . import builders
from .builders import GUID_A, GUID_B

SHA_ONE = hashlib.sha1(b"one").digest()
SHA_TWO = hashlib.sha1(b"two").digest()


def _body(feature_level=18, file_list_version=2, data_version=2):
    chunks = [(GUID_A, 0x0123456789ABCDEF, 7), (GUID_B, 42, 99)]
    files = [
        ("Plugin.uplugin", SHA_ONE, [(GUID_A, 0, 10)]),
        ("Source/Ünïcode.cpp", SHA_TWO, [(GUID_A, 10, 5), (GUID_B, 0, 20)]),
    ]
    return (
        builders.meta(feature_level, "1.2.3", data_version)
        + builders.chunk_list(chunks, feature_level)
        + builders.file_list(files, file_list_version)
        + builders.custom_fields()
    )


class BinaryManifestTests(unittest.TestCase):
    def test_reads_compressed_manifest(self):
        manifest = parse_manifest(builders.binary_manifest(_body()))

        self.assertEqual(manifest.feature_level, 18)
        self.assertEqual(manifest.build_version, "1.2.3")
        self.assertEqual(manifest.chunks[GUID_A], ChunkInfo(GUID_A, 0x0123456789ABCDEF, 7, None))
        self.assertEqual(manifest.chunks[GUID_B], ChunkInfo(GUID_B, 42, 99, None))
        self.assertEqual([f.filename for f in manifest.files], ["Plugin.uplugin", "Source/Ünïcode.cpp"])
        self.assertEqual(manifest.files[0].sha1, SHA_ONE)
        self.assertEqual(manifest.files[1].parts, (ChunkPart(GUID_A, 10, 5), ChunkPart(GUID_B, 0, 20)))
        self.assertEqual(manifest.files[1].size, 25)

    def test_reads_uncompressed_manifest(self):
        manifest = parse_manifest(builders.binary_manifest(_body(), compressed=False))

        self.assertEqual(len(manifest.files), 2)

    def test_reads_older_section_versions(self):
        manifest = parse_manifest(builders.binary_manifest(_body(file_list_version=0, data_version=0)))

        self.assertEqual(manifest.files[1].parts[1], ChunkPart(GUID_B, 0, 20))

    def test_skips_unknown_trailing_metadata(self):
        body = (
            builders.meta(18, "9.9", extra=b"future-field")
            + builders.chunk_list([(GUID_A, 1, 2)], 18)
            + builders.file_list([("a.txt", SHA_ONE, [(GUID_A, 0, 3)])])
        )

        manifest = parse_manifest(builders.binary_manifest(body))

        self.assertEqual(manifest.build_version, "9.9")
        self.assertEqual(manifest.files[0].parts, (ChunkPart(GUID_A, 0, 3),))

    def test_reads_feature_level_22_chunk_secrets(self):
        manifest = parse_manifest(builders.binary_manifest(_body(feature_level=22)))

        self.assertEqual(manifest.chunks[GUID_A].secret_guid, (1, 2, 3, 4))
        self.assertEqual(manifest.files[0].filename, "Plugin.uplugin")

    def test_rejects_hash_mismatch(self):
        data = builders.binary_manifest(_body(), sha1=b"\0" * 20)

        with self.assertRaisesRegex(FormatError, "hash"):
            parse_manifest(data)

    def test_rejects_encrypted_manifest(self):
        data = builders.binary_manifest(_body(), stored_as_extra=0x2)

        with self.assertRaisesRegex(FormatError, "Encrypted"):
            parse_manifest(data)

    def test_rejects_truncated_manifest(self):
        data = builders.binary_manifest(_body(), compressed=False)

        with self.assertRaises(FormatError):
            parse_manifest(data[:-60])


class JsonManifestTests(unittest.TestCase):
    def test_reads_json_manifest(self):
        data = builders.json_manifest(
            17,
            [(GUID_A, 0x0123456789ABCDEF, 7)],
            [("Content/Map.umap", SHA_ONE, [(GUID_A, 5, 100)])],
        )

        manifest = parse_manifest(data)

        self.assertEqual(manifest.feature_level, 17)
        self.assertEqual(manifest.build_version, "1.0.0")
        self.assertEqual(manifest.chunks[GUID_A], ChunkInfo(GUID_A, 0x0123456789ABCDEF, 7))
        self.assertEqual(manifest.files[0].filename, "Content/Map.umap")
        self.assertEqual(manifest.files[0].sha1, SHA_ONE)
        self.assertEqual(manifest.files[0].parts, (ChunkPart(GUID_A, 5, 100),))

    def test_rejects_invalid_json(self):
        with self.assertRaises(FormatError):
            parse_manifest(b"not a manifest")

    def test_rejects_json_without_chunk_lists(self):
        with self.assertRaises(FormatError):
            parse_manifest(b"{}")


class ChunkPathTests(unittest.TestCase):
    chunk = ChunkInfo(GUID_A, 0x0123456789ABCDEF, 7)

    def test_feature_level_15_to_21_uses_chunks_v4(self):
        self.assertEqual(
            chunk_path(self.chunk, 18),
            "ChunksV4/07/0123456789ABCDEF_11111111222222223333333344444444.chunk",
        )

    def test_older_feature_levels_use_older_folders(self):
        self.assertTrue(chunk_path(self.chunk, 14).startswith("ChunksV3/"))
        self.assertTrue(chunk_path(self.chunk, 5).startswith("ChunksV2/"))
        self.assertTrue(chunk_path(self.chunk, 2).startswith("Chunks/"))

    def test_feature_level_22_uses_base64_names(self):
        self.assertEqual(chunk_path(self.chunk, 22), "ChunksV5/plain/07/782riWdFIwE_ERERESIiIiIzMzMzRERERA.chunk")

    def test_feature_level_22_with_secret(self):
        chunk = ChunkInfo(GUID_A, 0x0123456789ABCDEF, 7, (1, 2, 3, 4))

        self.assertEqual(
            chunk_path(chunk, 22),
            "ChunksV5/AQAAAAIAAAADAAAABAAAAA/07/782riWdFIwE_ERERESIiIiIzMzMzRERERA.chunk",
        )


class DecodeChunkTests(unittest.TestCase):
    def test_decodes_compressed_chunk(self):
        self.assertEqual(decode_chunk(builders.chunk_file(GUID_A, b"payload")), (GUID_A, b"payload"))

    def test_decodes_uncompressed_chunk(self):
        self.assertEqual(decode_chunk(builders.chunk_file(GUID_B, b"raw", compressed=False)), (GUID_B, b"raw"))

    def test_rejects_encrypted_chunk(self):
        with self.assertRaisesRegex(FormatError, "Encrypted"):
            decode_chunk(builders.chunk_file(GUID_A, b"secret", encrypted=True))

    def test_rejects_bad_magic(self):
        with self.assertRaisesRegex(FormatError, "magic"):
            decode_chunk(b"\0" * 80)

    def test_rejects_corrupt_payload(self):
        data = builders.chunk_file(GUID_A, b"payload")

        with self.assertRaises(FormatError):
            decode_chunk(data[:66] + b"garbage")

    def test_rejects_truncated_header(self):
        with self.assertRaises(FormatError):
            decode_chunk(builders.chunk_file(GUID_A, b"payload")[:20])


if __name__ == "__main__":
    unittest.main()
