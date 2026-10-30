import hashlib
import tempfile
import unittest
from pathlib import Path

from fabdl.download import STAGING_SUFFIX, DownloadError, download_build, safe_relative_path
from fabdl.manifest import ChunkInfo, ChunkPart, FileEntry, Manifest, chunk_path
from fabdl.net import HttpError

from . import builders
from .builders import GUID_A, GUID_B

CHUNK_A = ChunkInfo(GUID_A, 1, 3)
CHUNK_B = ChunkInfo(GUID_B, 2, 4)
PAYLOAD_A = b"HelloWorld" + b"\0" * 6
PAYLOAD_B = b"0123456789"


def _manifest(files):
    return Manifest(18, "1.0", {GUID_A: CHUNK_A, GUID_B: CHUNK_B}, tuple(files))


def _entry(name, parts, content):
    return FileEntry(name, hashlib.sha1(content).digest(), tuple(parts))


class FakeCdn:
    def __init__(self, base_urls):
        self.files = {}
        for base_url in base_urls:
            self.files[f"{base_url}/{chunk_path(CHUNK_A, 18)}"] = builders.chunk_file(GUID_A, PAYLOAD_A)
            self.files[f"{base_url}/{chunk_path(CHUNK_B, 18)}"] = builders.chunk_file(GUID_B, PAYLOAD_B)
        self.failing = set()
        self.requests = []

    def fetch(self, url):
        self.requests.append(url)
        if any(url.startswith(base) for base in self.failing):
            raise HttpError("GET", url, 503, b"")
        if url not in self.files:
            raise HttpError("GET", url, 404, b"")
        return self.files[url]


class DownloadBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "Plugin UE_5.3"

    def tearDown(self):
        self.temp.cleanup()

    def test_assembles_files_from_shared_chunks(self):
        cdn = FakeCdn(["https://cdn"])
        manifest = _manifest([
            _entry("Plugin.uplugin", [ChunkPart(GUID_A, 0, 5)], b"Hello"),
            _entry("Source/Mixed.cpp", [ChunkPart(GUID_A, 5, 5), ChunkPart(GUID_B, 2, 3)], b"World234"),
        ])
        progress = []

        paths = download_build(manifest, ["https://cdn"], self.out, cdn.fetch, report=lambda d, t: progress.append((d, t)))

        self.assertEqual((self.out / "Plugin.uplugin").read_bytes(), b"Hello")
        self.assertEqual((self.out / "Source" / "Mixed.cpp").read_bytes(), b"World234")
        self.assertEqual(paths, [self.out / "Plugin.uplugin", self.out / "Source" / "Mixed.cpp"])
        self.assertEqual(len(cdn.requests), 2, "each chunk is downloaded once")
        self.assertEqual(progress[-1], (2, 2))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["Plugin UE_5.3"], "staging is removed")

    def test_writes_empty_files(self):
        manifest = _manifest([_entry("Empty.txt", [], b"")])

        download_build(manifest, ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

        self.assertEqual((self.out / "Empty.txt").read_bytes(), b"")

    def test_falls_back_to_the_next_cdn(self):
        cdn = FakeCdn(["https://one", "https://two"])
        cdn.failing.add("https://one")
        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])

        download_build(manifest, ["https://one", "https://two"], self.out, cdn.fetch)

        self.assertEqual((self.out / "a.txt").read_bytes(), b"0123")

    def test_fails_after_every_attempt_and_leaves_nothing(self):
        cdn = FakeCdn(["https://one"])
        cdn.failing.add("https://one")
        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])

        with self.assertRaisesRegex(DownloadError, "Could not download chunk"):
            download_build(manifest, ["https://one"], self.out, cdn.fetch)

        self.assertEqual(len(cdn.requests), 3)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_a_file_that_fails_its_hash(self):
        manifest = _manifest([FileEntry("a.txt", b"\0" * 20, (ChunkPart(GUID_B, 0, 4),))])

        with self.assertRaisesRegex(DownloadError, "SHA-1"):
            download_build(manifest, ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_a_chunk_with_the_wrong_guid(self):
        cdn = FakeCdn(["https://cdn"])
        cdn.files[f"https://cdn/{chunk_path(CHUNK_B, 18)}"] = builders.chunk_file(GUID_A, PAYLOAD_A)
        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])

        with self.assertRaisesRegex(DownloadError, "contains chunk"):
            download_build(manifest, ["https://cdn"], self.out, cdn.fetch)

    def test_rejects_a_part_outside_its_chunk(self):
        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 8, 4)], b"89\0\0")])

        with self.assertRaisesRegex(DownloadError, "outside"):
            download_build(manifest, ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

    def test_rejects_unknown_chunks(self):
        unknown = (9, 9, 9, 9)
        manifest = _manifest([_entry("a.txt", [ChunkPart(unknown, 0, 1)], b"x")])

        with self.assertRaisesRegex(DownloadError, "unknown chunks"):
            download_build(manifest, ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

    def test_refuses_an_existing_output_folder(self):
        self.out.mkdir()

        with self.assertRaisesRegex(DownloadError, "already exists"):
            download_build(_manifest([]), ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

    def test_replaces_a_leftover_staging_folder(self):
        leftover = self.root / ("Plugin UE_5.3" + STAGING_SUFFIX)
        (leftover / "files").mkdir(parents=True)
        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])

        download_build(manifest, ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

        self.assertFalse(leftover.exists())
        self.assertEqual((self.out / "a.txt").read_bytes(), b"0123")

    def test_checks_paths_before_downloading(self):
        cdn = FakeCdn(["https://cdn"])
        manifest = _manifest([_entry("../evil.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])

        with self.assertRaisesRegex(DownloadError, "unsafe"):
            download_build(manifest, ["https://cdn"], self.out, cdn.fetch)

        self.assertEqual(cdn.requests, [])


class SafeRelativePathTests(unittest.TestCase):
    def test_accepts_nested_paths(self):
        self.assertEqual(safe_relative_path("Source/Private/A.cpp").parts, ("Source", "Private", "A.cpp"))
        self.assertEqual(safe_relative_path("Source\\A.cpp").parts, ("Source", "A.cpp"))

    def test_rejects_paths_leaving_the_folder(self):
        for name in ["", "../a", "a/../../b", "/etc/passwd", "C:/Windows/a.dll", "C:a", "..\\a", "file.txt:stream"]:
            with self.subTest(name=name), self.assertRaises(DownloadError):
                safe_relative_path(name)


if __name__ == "__main__":
    unittest.main()
