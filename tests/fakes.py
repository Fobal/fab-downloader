from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fabdl.auth import SessionStore
from fabdl.net import HttpClient, HttpError


def _encode(payload: object) -> bytes:
    return payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class FakeHttp(HttpClient):
    """Answers requests from a queue of (status, body) responses and records each request."""

    def __init__(self, *responses: tuple[int, object]):
        super().__init__()
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        status, payload = self.responses.pop(0)
        data = _encode(payload)
        if status >= 400:
            raise HttpError(method, url, status, data)
        return data


class RoutedHttp(HttpClient):
    """Answers each "METHOD url" from a fixed table, safe to call from several threads."""

    def __init__(self, routes: dict[str, object]):
        super().__init__()
        self.routes = routes
        self.requests: list[str] = []
        self._lock = threading.Lock()

    def request(self, method, url, *, headers=None, body=None):
        key = f"{method} {url}"
        with self._lock:
            self.requests.append(key)
        if key not in self.routes:
            raise HttpError(method, url, 404, b"")
        return _encode(self.routes[key])


class MemoryStore(SessionStore):
    def __init__(self, data=None):
        super().__init__(Path("unused"))
        self.data = data

    def load(self):
        return self.data

    def save(self, data):
        self.data = data

    def clear(self):
        self.data = None


def make_project(root: Path, *, installed_engine: bool = False, version: tuple[int, int] = (5, 3)) -> Path:
    """Create an engine tree with a project inside it, so no engine lookup is needed. Returns the .uproject."""
    build = root / "Engine" / "Build"
    build.mkdir(parents=True)
    (build / "Build.version").write_text(json.dumps({"MajorVersion": version[0], "MinorVersion": version[1]}))
    if installed_engine:
        (build / "InstalledBuild.txt").write_text("")
    project = root / "Games" / "Game" / "Game.uproject"
    project.parent.mkdir(parents=True)
    project.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": ""}))
    return project


def make_plugin(folder: Path, name: str, *, binaries: bool = True) -> Path:
    """Create a plugin folder with a descriptor, source and optional prebuilt folders."""
    (folder / "Source" / name).mkdir(parents=True)
    (folder / f"{name}.uplugin").write_text("{}")
    (folder / "Source" / name / f"{name}.cpp").write_text("// code")
    if binaries:
        (folder / "Binaries" / "Win64").mkdir(parents=True)
        (folder / "Binaries" / "Win64" / f"UnrealEditor-{name}.dll").write_text("dll")
        (folder / "Intermediate").mkdir()
    return folder


def token_response(access="access1", refresh="refresh1", expires_at=None):
    expires_at = expires_at or datetime.now(timezone.utc) + timedelta(hours=8)
    return {
        "access_token": access,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "refresh_token": refresh,
        "account_id": "account1",
        "displayName": "Me",
    }
