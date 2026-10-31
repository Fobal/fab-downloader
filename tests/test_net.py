# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
import ssl
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from fabdl import net
from fabdl.net import HttpClient


class MacOSRootsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.roots = Path(temp.name) / "cert.pem"
        self.roots.write_text("roots")
        self._patch(net, "MACOS_ROOTS", new=self.roots)
        self.context = mock.Mock()
        self.context.cert_store_stats.return_value = {"x509_ca": 0}
        self.create_context = self._patch(net.ssl, "create_default_context", return_value=self.context)

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _on(self, platform):
        return mock.patch.object(net.sys, "platform", platform)

    def test_trusts_the_macos_roots_when_openssl_has_none(self):
        with self._on("darwin"):
            self.assertIs(net.macos_tls_context(), self.context)

        self.context.load_verify_locations.assert_called_once_with(cafile=str(self.roots))

    def test_keeps_the_default_when_openssl_has_roots(self):
        self.context.cert_store_stats.return_value = {"x509_ca": 140}

        with self._on("darwin"):
            self.assertIsNone(net.macos_tls_context())

        self.context.load_verify_locations.assert_not_called()

    def test_keeps_the_default_without_the_macos_roots(self):
        self.roots.unlink()

        with self._on("darwin"):
            self.assertIsNone(net.macos_tls_context())

    def test_only_changes_macos(self):
        for platform in ("win32", "linux"):
            with self._on(platform):
                self.assertIsNone(net.macos_tls_context())

        self.create_context.assert_not_called()

    def test_the_client_uses_it(self):
        with self._on("darwin"):
            client = HttpClient()

        handlers = [h for h in client._opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual([h._context for h in handlers], [self.context])


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class RealMacOSRootsTests(unittest.TestCase):
    def test_macos_has_the_roots_file(self):
        context = ssl.create_default_context(cafile=str(net.MACOS_ROOTS))

        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)


if __name__ == "__main__":
    unittest.main()
