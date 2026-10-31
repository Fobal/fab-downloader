import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fabdl import download
from fabdl.download import (
    RESUME_HINT,
    STAGING_SUFFIX,
    DownloadCancelled,
    DownloadError,
    download_build,
    safe_relative_path,
)
from fabdl.manifest import ChunkInfo, ChunkPart, FileEntry, Manifest, chunk_path, guid_hex
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
        self.staging = self.root / ("Plugin UE_5.3" + STAGING_SUFFIX)
        # Three rounds of attempts, without the real waits between them.
        patcher = mock.patch.object(download, "RETRY_DELAYS", (0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def _stored_chunks(self):
        return sorted(path.name for path in (self.staging / "chunks").iterdir())

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
        self.assertEqual(list(self.root.iterdir()), [], "no chunk arrived, so there is nothing to keep")

    def test_waits_between_rounds_of_attempts(self):
        cdn = FakeCdn(["https://cdn"])
        failures = iter([True, True])

        def flaky_fetch(url):
            if next(failures, False):
                raise OSError("connection reset")
            return cdn.fetch(url)

        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])
        started = time.monotonic()

        download_build(manifest, ["https://cdn"], self.out, flaky_fetch, retry_delays=(0.1, 0.2))

        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual((self.out / "a.txt").read_bytes(), b"0123")

    def test_keeps_the_chunks_of_a_failed_download_and_resumes(self):
        cdn = FakeCdn(["https://cdn"])
        chunk_b_url = f"https://cdn/{chunk_path(CHUNK_B, 18)}"
        chunk_b = cdn.files.pop(chunk_b_url)
        manifest = _manifest([
            _entry("a.txt", [ChunkPart(GUID_A, 0, 5)], b"Hello"),
            _entry("b.txt", [ChunkPart(GUID_B, 0, 4)], b"0123"),
        ])

        with self.assertRaises(DownloadError) as failure:
            download_build(manifest, ["https://cdn"], self.out, cdn.fetch)

        self.assertIn(RESUME_HINT, str(failure.exception))
        self.assertFalse(self.out.exists())
        self.assertEqual(self._stored_chunks(), [f"{guid_hex(GUID_A)}.bin"])
        self.assertFalse((self.staging / "files").exists())

        cdn.files[chunk_b_url] = chunk_b
        cdn.requests.clear()
        progress = []
        download_build(manifest, ["https://cdn"], self.out, cdn.fetch, report=lambda d, t: progress.append((d, t)))

        self.assertEqual(cdn.requests, [chunk_b_url], "only the missing chunk is downloaded")
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual((self.out / "a.txt").read_bytes(), b"Hello")
        self.assertEqual((self.out / "b.txt").read_bytes(), b"0123")
        self.assertFalse(self.staging.exists())

    def test_resumes_after_the_program_was_stopped(self):
        chunks = self.staging / "chunks"
        chunks.mkdir(parents=True)
        (chunks / f"{guid_hex(GUID_A)}.bin").write_bytes(PAYLOAD_A)
        (chunks / f"{guid_hex(GUID_B)}.tmp").write_bytes(b"half a chunk")
        (self.staging / "files").mkdir()
        (self.staging / "files" / "a.txt").write_bytes(b"Hel")
        cdn = FakeCdn(["https://cdn"])
        manifest = _manifest([
            _entry("a.txt", [ChunkPart(GUID_A, 0, 5)], b"Hello"),
            _entry("b.txt", [ChunkPart(GUID_B, 0, 4)], b"0123"),
        ])

        download_build(manifest, ["https://cdn"], self.out, cdn.fetch)

        self.assertEqual(cdn.requests, [f"https://cdn/{chunk_path(CHUNK_B, 18)}"])
        self.assertEqual((self.out / "a.txt").read_bytes(), b"Hello")
        self.assertEqual((self.out / "b.txt").read_bytes(), b"0123")
        self.assertFalse(self.staging.exists())

    def test_cancelling_removes_what_was_downloaded(self):
        cdn = FakeCdn(["https://cdn"])
        stop = threading.Event()

        def fetch_then_cancel(url):
            stop.set()
            return cdn.fetch(url)

        manifest = _manifest([
            _entry("a.txt", [ChunkPart(GUID_A, 0, 5)], b"Hello"),
            _entry("b.txt", [ChunkPart(GUID_B, 0, 4)], b"0123"),
        ])

        with self.assertRaises(DownloadCancelled):
            download_build(manifest, ["https://cdn"], self.out, fetch_then_cancel, stop=stop)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancelled_before_starting_downloads_nothing(self):
        cdn = FakeCdn(["https://cdn"])
        stop = threading.Event()
        stop.set()
        manifest = _manifest([_entry("a.txt", [ChunkPart(GUID_B, 0, 4)], b"0123")])

        with self.assertRaises(DownloadCancelled):
            download_build(manifest, ["https://cdn"], self.out, cdn.fetch, stop=stop)

        self.assertEqual(cdn.requests, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_a_file_that_fails_its_hash(self):
        manifest = _manifest([FileEntry("a.txt", b"\0" * 20, (ChunkPart(GUID_B, 0, 4),))])

        with self.assertRaisesRegex(DownloadError, "SHA-1"):
            download_build(manifest, ["https://cdn"], self.out, FakeCdn(["https://cdn"]).fetch)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_fetches_the_chunks_of_a_file_that_failed_its_hash_again(self):
        cdn = FakeCdn(["https://cdn"])
        chunk_b_url = f"https://cdn/{chunk_path(CHUNK_B, 18)}"
        good_chunk = cdn.files[chunk_b_url]
        cdn.files[chunk_b_url] = builders.chunk_file(GUID_B, b"9999999999")
        manifest = _manifest([
            _entry("a.txt", [ChunkPart(GUID_A, 0, 5)], b"Hello"),
            _entry("b.txt", [ChunkPart(GUID_B, 0, 4)], b"0123"),
        ])

        with self.assertRaisesRegex(DownloadError, "b.txt failed its SHA-1 check"):
            download_build(manifest, ["https://cdn"], self.out, cdn.fetch)

        self.assertEqual(self._stored_chunks(), [f"{guid_hex(GUID_A)}.bin"], "the bad chunk is dropped")
        cdn.files[chunk_b_url] = good_chunk
        cdn.requests.clear()

        download_build(manifest, ["https://cdn"], self.out, cdn.fetch)

        self.assertEqual(cdn.requests, [chunk_b_url])
        self.assertEqual((self.out / "b.txt").read_bytes(), b"0123")

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
