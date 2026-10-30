"""Epic account login through the Epic Games Launcher's OAuth client.

The launcher client id and secret are public; they identify the launcher, not an account.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from . import dpapi
from .net import REQUEST_ERRORS, HttpClient, HttpError
from .settings import APP_DATA

CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
OAUTH_HOST = "https://account-public-service-prod03.ol.epicgames.com"
LOGIN_URL = "https://www.epicgames.com/id/login?redirectUrl=" + quote(
    f"https://www.epicgames.com/id/api/redirect?clientId={CLIENT_ID}&responseType=code", safe=""
)
_REFRESH_MARGIN = timedelta(minutes=10)
# Epic's codes are 32 hex characters; any alphanumeric code is accepted in case that changes.
_CODE_RE = re.compile(r"[A-Za-z0-9]+")


class AuthError(Exception):
    """Epic rejected a login or token request."""


@dataclass(frozen=True)
class Session:
    access_token: str
    expires_at: datetime
    refresh_token: str
    account_id: str
    display_name: str

    @classmethod
    def from_token_response(cls, data: dict) -> Session:
        return cls(
            access_token=data["access_token"],
            expires_at=_parse_time(data["expires_at"]),
            refresh_token=data["refresh_token"],
            account_id=data["account_id"],
            display_name=data.get("displayName") or "",
        )


class SessionStore:
    """Keeps Epic's token response on disk.

    On Windows it is encrypted for the current user. Elsewhere there is no DPAPI, so it is plain
    JSON that only the owner may read, the way ssh keys and the GitHub CLI's token are kept.
    """

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def default(cls) -> SessionStore:
        return cls(APP_DATA / "session.bin")

    def load(self) -> dict | None:
        try:
            data = self.path.read_bytes()
            data = json.loads(dpapi.unprotect(data) if sys.platform == "win32" else data)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = json.dumps(data).encode()
        if sys.platform == "win32":
            self.path.write_bytes(dpapi.protect(text))
            return
        # A new file gets mode 0600 from the start, whatever an older file allowed.
        self.path.unlink(missing_ok=True)
        with os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(text)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def get_session(
    http: HttpClient,
    store: SessionStore,
    *,
    prompt: Callable[[str], str] = input,
    open_browser: Callable[[str], object] = webbrowser.open,
    now: datetime | None = None,
) -> Session:
    """Reuse or refresh the saved session, or sign in through the browser."""
    session = load_session(http, store, now=now)
    if session:
        return session
    print("Sign in to Epic in your browser. If it does not open, visit:")
    print(f"  {LOGIN_URL}")
    print('After signing in, the page shows JSON text. Copy the "authorizationCode" value.')
    open_browser(LOGIN_URL)
    return sign_in(http, store, prompt("Authorization code: "))


def load_session(http: HttpClient, store: SessionStore, *, now: datetime | None = None) -> Session | None:
    """Return the saved session, refreshed if it is about to expire, or None if a sign-in is needed."""
    now = now or datetime.now(timezone.utc)
    saved = store.load()
    if not saved:
        return None
    try:
        session = Session.from_token_response(saved)
    except (KeyError, TypeError, ValueError):
        return None
    if session.expires_at - now > _REFRESH_MARGIN:
        return session
    try:
        refreshed = _token_request(http, {"grant_type": "refresh_token", "refresh_token": session.refresh_token})
        return _save(store, refreshed)
    except AuthError:
        return None  # The refresh token expired or was revoked.


def sign_in(http: HttpClient, store: SessionStore, code_text: str) -> Session:
    """Exchange the code from Epic's login page for a session and save it."""
    code = parse_authorization_code(code_text)
    return _save(store, _token_request(http, {"grant_type": "authorization_code", "code": code}))


def logout(http: HttpClient, store: SessionStore) -> bool:
    """End the saved session. Returns False if there was none."""
    saved = store.load()
    store.clear()
    if not saved or not saved.get("access_token"):
        return False
    token = saved["access_token"]
    try:
        http.request(
            "DELETE",
            f"{OAUTH_HOST}/account/api/oauth/sessions/kill/{quote(token, safe='')}",
            headers={"Authorization": f"bearer {token}"},
        )
    except REQUEST_ERRORS:
        pass  # The token may already have expired; the local copy is gone either way.
    return True


def parse_authorization_code(text: str) -> str:
    """Accept the bare code or the whole JSON page Epic shows after signing in."""
    text = text.strip()
    if text.startswith("{"):
        try:
            code = json.loads(text).get("authorizationCode")
        except (ValueError, AttributeError):
            code = None
    else:
        code = text.strip("\"' ")
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise AuthError('That is not an authorization code. Copy the "authorizationCode" value from the page.')
    return code


def _token_request(http: HttpClient, form: dict[str, str]) -> dict:
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    try:
        data = http.json(
            "POST",
            f"{OAUTH_HOST}/account/api/oauth/token",
            headers={"Authorization": f"basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({**form, "token_type": "eg1"}).encode(),
        )
    except HttpError as error:
        try:
            details = json.loads(error.body)
        except ValueError:
            details = None
        raise AuthError(_epic_error_message(details) or str(error)) from None
    if not isinstance(data, dict) or "errorCode" in data:
        raise AuthError(_epic_error_message(data) or "Epic rejected the login.")
    return data


def _epic_error_message(details: object) -> str:
    if not isinstance(details, dict):
        return ""
    message = details.get("errorMessage") or details.get("errorCode") or ""
    if message and details.get("continuationUrl"):
        message += f" Open {details['continuationUrl']} to continue."
    return message


def _save(store: SessionStore, data: dict) -> Session:
    try:
        session = Session.from_token_response(data)
    except (KeyError, TypeError, ValueError) as error:
        raise AuthError(f"Epic's token response is missing {error}.") from None
    store.save(data)
    return session


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
