# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Small urllib wrapper shared by the Epic, Fab and CDN requests."""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import urllib.error
import urllib.request
from typing import Any

# Epic's services expect a launcher user agent.
LAUNCHER_USER_AGENT = "UELauncher/17.0.1-37584233+++Portal+Release-Live Windows/10.0.19043.1.0.64bit"


class HttpError(Exception):
    """The server answered with an error status."""

    def __init__(self, method: str, url: str, status: int, body: bytes):
        # Drop the query string: signed CDN URLs carry their signature there.
        super().__init__(f"{method} {url.split('?', 1)[0]} failed: HTTP {status}")
        self.status = status
        self.body = body


# Everything a request can raise: error statuses, network failures and broken responses.
REQUEST_ERRORS = (HttpError, OSError, http.client.HTTPException)


class HttpClient:
    def __init__(self, timeout: float = 60):
        self._timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> bytes:
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"User-Agent": LAUNCHER_USER_AGENT, **(headers or {})},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise HttpError(method, url, error.code, error.read()) from None

    def json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Any:
        data = self.request(
            method, url, headers={"Accept": "application/json", **(headers or {})}, body=body
        )
        return json.loads(data)
