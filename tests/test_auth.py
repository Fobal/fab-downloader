import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fabdl.auth import (
    LOGIN_URL,
    AuthError,
    SessionStore,
    get_session,
    load_session,
    logout,
    parse_authorization_code,
    sign_in,
)

from .fakes import FakeHttp, MemoryStore, token_response

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _token(access="access1", refresh="refresh1", expires_in=timedelta(hours=8)):
    return token_response(access, refresh, NOW + expires_in)


class ParseAuthorizationCodeTests(unittest.TestCase):
    def test_accepts_the_bare_code(self):
        self.assertEqual(parse_authorization_code("  0123456789abcdef0123456789abcdef \n"), "0123456789abcdef0123456789abcdef")
        self.assertEqual(parse_authorization_code('"abc123"'), "abc123")

    def test_accepts_the_whole_page(self):
        page = '{"warning":"Do not share","redirectUrl":"https://x","authorizationCode":"abc123","exchangeCode":null,"sid":null}'

        self.assertEqual(parse_authorization_code(page), "abc123")

    def test_rejects_anything_else(self):
        for text in ["", '{"authorizationCode": null}', "{broken", "two words", "[]"]:
            with self.subTest(text=text), self.assertRaises(AuthError):
                parse_authorization_code(text)


class GetSessionTests(unittest.TestCase):
    def _get(self, http, store, code="code1"):
        opened = []
        with contextlib.redirect_stdout(io.StringIO()):
            session = get_session(http, store, prompt=lambda _: code, open_browser=opened.append, now=NOW)
        return session, opened

    def test_reuses_a_valid_session(self):
        http = FakeHttp()

        session, opened = self._get(http, MemoryStore(_token()))

        self.assertEqual(session.access_token, "access1")
        self.assertEqual(session.account_id, "account1")
        self.assertEqual(http.requests, [])
        self.assertEqual(opened, [])

    def test_refreshes_a_session_about_to_expire(self):
        store = MemoryStore(_token(expires_in=timedelta(minutes=5)))
        http = FakeHttp((200, _token(access="access2", refresh="refresh2")))

        session, opened = self._get(http, store)

        self.assertEqual(session.access_token, "access2")
        self.assertEqual(store.data["refresh_token"], "refresh2")
        form = parse_qs(http.requests[0]["body"].decode())
        self.assertEqual(form, {"grant_type": ["refresh_token"], "refresh_token": ["refresh1"], "token_type": ["eg1"]})
        self.assertTrue(http.requests[0]["headers"]["Authorization"].startswith("basic "))
        self.assertEqual(opened, [])

    def test_signs_in_when_the_refresh_is_rejected(self):
        store = MemoryStore(_token(expires_in=timedelta(hours=-1)))
        http = FakeHttp(
            (400, {"errorCode": "errors.com.epicgames.account.auth_token.invalid_refresh_token"}),
            (200, _token(access="access3")),
        )

        session, opened = self._get(http, store)

        self.assertEqual(session.access_token, "access3")
        self.assertEqual(opened, [LOGIN_URL])
        form = parse_qs(http.requests[1]["body"].decode())
        self.assertEqual(form, {"grant_type": ["authorization_code"], "code": ["code1"], "token_type": ["eg1"]})

    def test_signs_in_without_a_saved_session(self):
        store = MemoryStore()

        session, opened = self._get(FakeHttp((200, _token())), store)

        self.assertEqual(session.display_name, "Me")
        self.assertEqual(store.data["access_token"], "access1")
        self.assertEqual(opened, [LOGIN_URL])

    def test_signs_in_when_the_saved_session_is_damaged(self):
        session, opened = self._get(FakeHttp((200, _token())), MemoryStore({"access_token": "only"}))

        self.assertEqual(session.access_token, "access1")
        self.assertEqual(opened, [LOGIN_URL])

    def test_reports_epic_errors(self):
        http = FakeHttp((400, {
            "errorCode": "errors.com.epicgames.oauth.corrective_action_required",
            "errorMessage": "Accept the new terms.",
            "continuationUrl": "https://epic/terms",
        }))

        with self.assertRaisesRegex(AuthError, r"Accept the new terms\. Open https://epic/terms to continue\."):
            self._get(http, MemoryStore())

    def test_reports_incomplete_token_responses(self):
        with self.assertRaisesRegex(AuthError, "missing"):
            self._get(FakeHttp((200, {"access_token": "a"})), MemoryStore())

    def test_login_url_returns_to_the_code_page(self):
        self.assertIn("redirect%3FclientId%3D34a02cf8f4414e29b15921876da36f9a%26responseType%3Dcode", LOGIN_URL)


class LoadSessionTests(unittest.TestCase):
    def test_returns_none_without_a_saved_session(self):
        self.assertIsNone(load_session(FakeHttp(), MemoryStore(), now=NOW))

    def test_returns_none_when_the_refresh_is_rejected(self):
        http = FakeHttp((400, {"errorCode": "errors.com.epicgames.account.auth_token.invalid_refresh_token"}))

        self.assertIsNone(load_session(http, MemoryStore(_token(expires_in=timedelta(0))), now=NOW))

    def test_lets_network_failures_through(self):
        def offline(*args, **kwargs):
            raise OSError("offline")

        http = FakeHttp()
        http.request = offline

        with self.assertRaises(OSError):
            load_session(http, MemoryStore(_token(expires_in=timedelta(0))), now=NOW)


class SignInTests(unittest.TestCase):
    def test_exchanges_the_pasted_page_for_a_session(self):
        store = MemoryStore()
        http = FakeHttp((200, _token()))

        session = sign_in(http, store, '{"authorizationCode":"abc123"}')

        self.assertEqual(session.access_token, "access1")
        self.assertEqual(store.data["access_token"], "access1")
        self.assertEqual(parse_qs(http.requests[0]["body"].decode())["code"], ["abc123"])

    def test_rejects_a_bad_code_without_calling_epic(self):
        http = FakeHttp()

        with self.assertRaises(AuthError):
            sign_in(http, MemoryStore(), "not a code")

        self.assertEqual(http.requests, [])


class LogoutTests(unittest.TestCase):
    def test_kills_and_forgets_the_session(self):
        store = MemoryStore(_token())
        http = FakeHttp((204, b""))

        self.assertTrue(logout(http, store))

        self.assertIsNone(store.data)
        self.assertEqual(http.requests[0]["method"], "DELETE")
        self.assertTrue(http.requests[0]["url"].endswith("/account/api/oauth/sessions/kill/access1"))

    def test_forgets_the_session_even_if_epic_refuses(self):
        store = MemoryStore(_token())

        self.assertTrue(logout(FakeHttp((401, b"")), store))
        self.assertIsNone(store.data)

    def test_reports_no_session(self):
        self.assertFalse(logout(FakeHttp(), MemoryStore()))


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp.name) / "nested" / "session.bin")

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trips(self):
        self.store.save(_token())
        self.store.save(_token("access2"))

        self.assertEqual(self.store.load(), _token("access2"))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows only")
    def test_encrypts_on_windows(self):
        self.store.save(_token())

        self.assertNotIn(b"access1", self.store.path.read_bytes())

    @unittest.skipIf(sys.platform == "win32", "Windows encrypts the file instead")
    def test_only_the_owner_can_read_it_elsewhere(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_bytes(b"old")
        self.store.path.chmod(0o644)

        self.store.save(_token())

        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_treats_unreadable_files_as_no_session(self):
        self.assertIsNone(self.store.load())
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_bytes(b"not encrypted")

        self.assertIsNone(self.store.load())

    def test_clear_removes_the_file(self):
        self.store.save(_token())
        self.store.clear()
        self.store.clear()

        self.assertFalse(self.store.path.exists())


if __name__ == "__main__":
    unittest.main()
