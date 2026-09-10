"""Expanded regression coverage for auth, cache, API client, and feed modules.

These tests target previously-uncovered branches (error handling, cache
fallbacks, pagination, and multi-source feed aggregation) across
``auth_utils``, ``cache_utils``, ``api.client``, and the specialized feed
generators. See ``tests/test_application.py`` for the original suite.
"""

import asyncio
import pickle
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from opds_abs.api import client as api_client
from opds_abs.utils import auth_utils, cache_utils
from opds_abs.utils.error_utils import AuthenticationError


# ---------------------------------------------------------------------------
# Shared fakes for aiohttp-based network calls.
# ---------------------------------------------------------------------------

ConnectionKeyStub = namedtuple("ConnectionKeyStub", ["host", "port", "ssl"])
RequestInfoStub = namedtuple("RequestInfoStub", ["real_url"])


def make_connector_error(message="Connection refused"):
    """Build a real aiohttp.ClientConnectorError usable in tests."""
    return aiohttp.ClientConnectorError(
        ConnectionKeyStub(host="localhost", port=80, ssl=False), OSError(message))


def make_response_error(status, message="error"):
    """Build a real aiohttp.ClientResponseError usable in tests."""
    return aiohttp.ClientResponseError(
        RequestInfoStub(real_url="http://test/api"), (), status=status, message=message)


class FakeAsyncResponse:
    """Minimal async context manager mimicking an aiohttp response."""

    def __init__(self, status=200, json_data=None, text_data=""):
        """Store the scripted status, JSON body, and text body."""
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def __aenter__(self):
        """Enter the async context, returning self like a real aiohttp response."""
        return self

    async def __aexit__(self, *exc):
        """Exit the async context without suppressing exceptions."""
        return None

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    def raise_for_status(self):
        if self.status >= 400:
            raise make_response_error(self.status)


class ScriptedSession:
    """Fake aiohttp.ClientSession that plays back scripted get/post results.

    Each scripted result is either a FakeAsyncResponse or an Exception
    instance (raised synchronously when the call is made).
    """

    def __init__(self, get_results=None, post_results=None):
        """Queue the scripted get/post results to play back in order."""
        self._get_results = list(get_results or [])
        self._post_results = list(post_results or [])
        self.get_calls = []
        self.post_calls = []

    async def __aenter__(self):
        """Enter the async context, returning self like a real aiohttp response."""
        return self

    async def __aexit__(self, *exc):
        """Exit the async context without suppressing exceptions."""
        return None

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        result = self._get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = self._post_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def scripted_session_factory(session):
    """Return a callable usable to patch aiohttp.ClientSession, ignoring args."""
    return lambda *args, **kwargs: session


# ---------------------------------------------------------------------------
# auth_utils
# ---------------------------------------------------------------------------

class PasswordAsApiKeyTests(unittest.IsolatedAsyncioTestCase):
    """Verify the "password might be an API key" detection helper."""

    async def test_short_password_skips_api_key_attempt(self):
        result = await auth_utils._try_password_as_api_key("alice", "short")
        self.assertIsNone(result)

    async def test_disabled_api_key_auth_skips_attempt(self):
        with patch.object(auth_utils, "API_KEY_AUTH_ENABLED", False):
            result = await auth_utils._try_password_as_api_key("alice", "x" * 40)
        self.assertIsNone(result)

    async def test_long_password_succeeds_as_api_key(self):
        with patch.object(
                auth_utils, "authenticate_with_api_key",
                new=AsyncMock(return_value=("token", "Alice"))) as mock_auth:
            result = await auth_utils._try_password_as_api_key("alice", "x" * 40)
        self.assertEqual(result, ("token", "Alice"))
        mock_auth.assert_awaited_once_with("alice", "x" * 40)

    async def test_falls_back_to_password_auth_when_api_key_invalid(self):
        with patch.object(
                auth_utils, "authenticate_with_api_key",
                new=AsyncMock(side_effect=AuthenticationError("not a key"))):
            result = await auth_utils._try_password_as_api_key("alice", "x" * 40)
        self.assertIsNone(result)


class LoginWithPasswordTests(unittest.IsolatedAsyncioTestCase):
    """Verify _login_with_password's success and failure paths."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    async def test_successful_login_caches_token(self):
        response = FakeAsyncResponse(
            200, {"user": {"token": "tok123", "username": "Alice"}})
        session = ScriptedSession(post_results=[response])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            result = await auth_utils._login_with_password("alice", "pw")
        self.assertEqual(result, ("tok123", "Alice"))
        self.assertEqual(auth_utils.TOKEN_CACHE["alice"], ("tok123", "Alice"))

    async def test_non_200_response_raises_authentication_error(self):
        response = FakeAsyncResponse(401, text_data="bad credentials")
        session = ScriptedSession(post_results=[response])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(AuthenticationError):
                await auth_utils._login_with_password("alice", "wrong")

    async def test_missing_user_key_raises_authentication_error(self):
        response = FakeAsyncResponse(200, {})
        session = ScriptedSession(post_results=[response])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(AuthenticationError):
                await auth_utils._login_with_password("alice", "pw")

    async def test_missing_token_raises_authentication_error(self):
        response = FakeAsyncResponse(200, {"user": {"username": "alice"}})
        session = ScriptedSession(post_results=[response])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(AuthenticationError):
                await auth_utils._login_with_password("alice", "pw")

    async def test_connection_error_raises_clear_message(self):
        session = ScriptedSession(post_results=[make_connector_error()])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(AuthenticationError) as raised:
                await auth_utils._login_with_password("alice", "pw")
        self.assertIn("Cannot connect to host", str(raised.exception))

    async def test_client_error_raises_clear_message(self):
        session = ScriptedSession(post_results=[aiohttp.ClientError("boom")])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(AuthenticationError) as raised:
                await auth_utils._login_with_password("alice", "pw")
        self.assertIn("boom", str(raised.exception))

    async def test_unexpected_error_is_wrapped(self):
        class ExplodingResponse(FakeAsyncResponse):
            async def json(self):
                raise ValueError("bad json")

        session = ScriptedSession(post_results=[ExplodingResponse(200)])
        with patch.object(auth_utils.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(AuthenticationError) as raised:
                await auth_utils._login_with_password("alice", "pw")
        self.assertIn("Authentication error", str(raised.exception))


class AuthenticateWithAudiobookshelfTests(unittest.IsolatedAsyncioTestCase):
    """Verify the top-level authentication dispatcher."""

    async def test_api_key_argument_uses_api_key_auth(self):
        with patch.object(
                auth_utils, "authenticate_with_api_key",
                new=AsyncMock(return_value=("tok", "Alice"))) as mock_auth:
            result = await auth_utils.authenticate_with_audiobookshelf(
                "alice", None, api_key="key123")
        self.assertEqual(result, ("tok", "Alice"))
        mock_auth.assert_awaited_once_with("alice", "key123")

    async def test_password_that_looks_like_api_key_is_tried_first(self):
        with patch.object(
                auth_utils, "authenticate_with_api_key",
                new=AsyncMock(return_value=("tok", "Alice"))), \
             patch.object(
                auth_utils, "_login_with_password",
                new=AsyncMock()) as mock_login:
            result = await auth_utils.authenticate_with_audiobookshelf("alice", "x" * 40)
        self.assertEqual(result, ("tok", "Alice"))
        mock_login.assert_not_awaited()

    async def test_falls_back_to_password_login_when_needed(self):
        with patch.object(
                auth_utils, "_login_with_password",
                new=AsyncMock(return_value=("tok", "Alice"))) as mock_login:
            result = await auth_utils.authenticate_with_audiobookshelf("alice", "short")
        self.assertEqual(result, ("tok", "Alice"))
        mock_login.assert_awaited_once_with("alice", "short")

    async def test_no_credentials_still_attempts_password_login(self):
        with patch.object(
                auth_utils, "_login_with_password",
                new=AsyncMock(return_value=("tok", "Alice"))) as mock_login:
            result = await auth_utils.authenticate_with_audiobookshelf("alice", "")
        self.assertEqual(result, ("tok", "Alice"))
        mock_login.assert_awaited_once()


class TryApiMeTests(unittest.IsolatedAsyncioTestCase):
    """Verify the /api/me API key verification helper."""

    async def test_successful_verification_returns_token_and_username(self):
        session = ScriptedSession(get_results=[FakeAsyncResponse(
            200, {"user": {"username": "alice"}})])
        result = await auth_utils._try_api_me(session, "apikey123")
        self.assertEqual(result, ("apikey123", "alice"))

    async def test_non_200_response_returns_none(self):
        session = ScriptedSession(get_results=[FakeAsyncResponse(404, text_data="nope")])
        result = await auth_utils._try_api_me(session, "apikey123")
        self.assertIsNone(result)

    async def test_response_without_user_key_returns_none(self):
        session = ScriptedSession(get_results=[FakeAsyncResponse(200, {})])
        result = await auth_utils._try_api_me(session, "apikey123")
        self.assertIsNone(result)


class TryApiAuthorizeTests(unittest.IsolatedAsyncioTestCase):
    """Verify the legacy /api/authorize API key verification helper."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    async def test_get_success_caches_token(self):
        session = ScriptedSession(get_results=[FakeAsyncResponse(
            200, {"user": {"username": "alice"}})])
        result = await auth_utils._try_api_authorize(session, "key123", "alice")
        self.assertEqual(result, ("key123", "alice"))
        self.assertEqual(auth_utils.TOKEN_CACHE["alice"], ("key123", "alice"))

    async def test_get_fails_post_fallback_succeeds(self):
        session = ScriptedSession(
            get_results=[FakeAsyncResponse(500, text_data="err")],
            post_results=[FakeAsyncResponse(200, {"user": {"username": "bob"}})],
        )
        result = await auth_utils._try_api_authorize(session, "key123", "bob")
        self.assertEqual(result, ("key123", "bob"))

    async def test_username_mismatch_logs_warning_but_succeeds(self):
        session = ScriptedSession(get_results=[FakeAsyncResponse(
            200, {"user": {"username": "bob"}})])
        result = await auth_utils._try_api_authorize(session, "key123", "someone_else")
        self.assertEqual(result, ("key123", "bob"))

    async def test_both_methods_failing_raises(self):
        session = ScriptedSession(
            get_results=[FakeAsyncResponse(500, text_data="err")],
            post_results=[FakeAsyncResponse(500, text_data="still failing")],
        )
        with self.assertRaises(AuthenticationError):
            await auth_utils._try_api_authorize(session, "key123", "alice")

    async def test_invalid_response_raises(self):
        session = ScriptedSession(get_results=[FakeAsyncResponse(200, {})])
        with self.assertRaises(AuthenticationError):
            await auth_utils._try_api_authorize(session, "key123", "alice")


class AuthenticateWithApiKeyTests(unittest.IsolatedAsyncioTestCase):
    """Verify authenticate_with_api_key's dispatch and error handling."""

    async def test_uses_api_me_result_when_available(self):
        with patch.object(
                auth_utils, "_try_api_me", new=AsyncMock(return_value=("k", "alice"))), \
             patch.object(auth_utils, "_try_api_authorize", new=AsyncMock()) as authorize:
            result = await auth_utils.authenticate_with_api_key("alice", "k")
        self.assertEqual(result, ("k", "alice"))
        authorize.assert_not_awaited()

    async def test_falls_back_to_authorize_when_api_me_returns_none(self):
        with patch.object(auth_utils, "_try_api_me", new=AsyncMock(return_value=None)), \
             patch.object(
                auth_utils, "_try_api_authorize",
                new=AsyncMock(return_value=("k", "alice"))) as authorize:
            result = await auth_utils.authenticate_with_api_key("alice", "k")
        self.assertEqual(result, ("k", "alice"))
        authorize.assert_awaited_once()

    async def test_connection_error_produces_clear_message(self):
        with patch.object(
                auth_utils, "_try_api_me", new=AsyncMock(side_effect=make_connector_error())):
            with self.assertRaises(AuthenticationError) as raised:
                await auth_utils.authenticate_with_api_key("alice", "k")
        self.assertIn("Cannot connect to host", str(raised.exception))

    async def test_client_error_produces_clear_message(self):
        with patch.object(
                auth_utils, "_try_api_me", new=AsyncMock(side_effect=aiohttp.ClientError("boom"))):
            with self.assertRaises(AuthenticationError) as raised:
                await auth_utils.authenticate_with_api_key("alice", "k")
        self.assertIn("boom", str(raised.exception))

    async def test_unexpected_error_is_wrapped(self):
        with patch.object(
                auth_utils, "_try_api_me", new=AsyncMock(side_effect=ValueError("weird"))):
            with self.assertRaises(AuthenticationError) as raised:
                await auth_utils.authenticate_with_api_key("alice", "k")
        self.assertIn("API key authentication error", str(raised.exception))


class ParseAuthHeaderTests(unittest.TestCase):
    """Verify Basic/Bearer header parsing edge cases."""

    def test_invalid_base64_returns_none_tuple(self):
        self.assertEqual(
            auth_utils._parse_basic_auth_header("abcde"), (None, None, None))

    def test_missing_colon_returns_none_tuple(self):
        import base64
        encoded = base64.b64encode(b"nodata").decode()
        self.assertEqual(auth_utils._parse_basic_auth_header(encoded), (None, None, None))

    def test_credential_looking_like_api_key_when_disabled_still_parses(self):
        import base64
        encoded = base64.b64encode(f"alice:{'x' * 40}".encode()).decode()
        with patch.object(auth_utils, "API_KEY_AUTH_ENABLED", False):
            result = auth_utils._parse_basic_auth_header(encoded)
        self.assertEqual(result, ("alice", "x" * 40, None))

    def test_bearer_disabled_returns_none_tuple(self):
        from tests.test_application import make_request
        with patch.object(auth_utils, "API_KEY_AUTH_ENABLED", False):
            result = auth_utils._parse_bearer_auth_header(make_request(), "token123")
        self.assertEqual(result, (None, None, None))

    def test_bearer_uses_x_username_header(self):
        from tests.test_application import make_request
        request = make_request(headers={"X-Username": "alice"})
        result = auth_utils._parse_bearer_auth_header(request, "token123")
        self.assertEqual(result, ("alice", None, "token123"))

    def test_bearer_without_username_uses_placeholder(self):
        from tests.test_application import make_request
        result = auth_utils._parse_bearer_auth_header(make_request(), "token123")
        self.assertEqual(result, ("api_key_user", None, "token123"))

    def test_malformed_authorization_header_returns_none_tuple(self):
        from tests.test_application import make_request
        request = make_request(headers={"Authorization": "Malformed"})
        self.assertEqual(
            auth_utils.get_credentials_from_request(request), (None, None, None))

    def test_unsupported_auth_type_returns_none_tuple(self):
        from tests.test_application import make_request
        request = make_request(headers={"Authorization": "Digest xyz"})
        self.assertEqual(
            auth_utils.get_credentials_from_request(request), (None, None, None))


class GetUserTokenTests(unittest.IsolatedAsyncioTestCase):
    """Verify token caching and retrieval logic."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    async def test_persistent_cache_hit_populates_memory_cache(self):
        with patch.object(auth_utils, "cache_get", return_value=("tok", "Alice")):
            result = await auth_utils.get_user_token("alice", "pw")
        self.assertEqual(result, ("tok", "Alice"))
        self.assertEqual(auth_utils.TOKEN_CACHE["alice"], ("tok", "Alice"))

    async def test_api_key_placeholder_username_is_replaced(self):
        with patch.object(
                auth_utils, "authenticate_with_api_key",
                new=AsyncMock(return_value=("tok", "RealUser"))):
            result = await auth_utils.get_user_token("api_key_user", None, "key123")
        self.assertEqual(result, ("tok", "RealUser"))
        self.assertIn("RealUser", auth_utils.TOKEN_CACHE)

    async def test_caching_disabled_does_not_populate_token_cache(self):
        with patch.object(auth_utils, "AUTH_TOKEN_CACHING", False), \
             patch.object(
                auth_utils, "authenticate_with_audiobookshelf",
                new=AsyncMock(return_value=("tok", "Alice"))), \
             patch.object(auth_utils, "cache_set") as mock_cache_set:
            result = await auth_utils.get_user_token("alice", "pw")
        self.assertEqual(result, ("tok", "Alice"))
        self.assertNotIn("alice", auth_utils.TOKEN_CACHE)
        mock_cache_set.assert_not_called()


class VerifyCredentialsTests(unittest.IsolatedAsyncioTestCase):
    """Verify the request-to-credentials verification pipeline."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    async def test_api_key_provided_but_disabled_raises(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=("alice", None, "key123")), \
             patch.object(auth_utils, "API_KEY_AUTH_ENABLED", False):
            with self.assertRaises(AuthenticationError):
                await auth_utils.verify_credentials(make_request())

    async def test_api_key_success_replaces_placeholder_username(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=("api_key_user", None, "key123")), \
             patch.object(
                auth_utils, "get_user_token",
                new=AsyncMock(return_value=("tok", "RealUser"))):
            result = await auth_utils.verify_credentials(make_request())
        self.assertEqual(result, ("RealUser", "tok", "RealUser"))

    async def test_api_key_failure_reraises(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=("alice", None, "key123")), \
             patch.object(
                auth_utils, "get_user_token",
                new=AsyncMock(side_effect=AuthenticationError("bad key"))):
            with self.assertRaises(AuthenticationError):
                await auth_utils.verify_credentials(make_request())

    async def test_password_success_returns_credentials(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=("alice", "pw", None)), \
             patch.object(
                auth_utils, "get_user_token",
                new=AsyncMock(return_value=("tok", "Alice"))):
            result = await auth_utils.verify_credentials(make_request())
        self.assertEqual(result, ("alice", "tok", "Alice"))

    async def test_password_failure_reraises(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=("alice", "pw", None)), \
             patch.object(
                auth_utils, "get_user_token",
                new=AsyncMock(side_effect=AuthenticationError("bad login"))):
            with self.assertRaises(AuthenticationError):
                await auth_utils.verify_credentials(make_request())

    async def test_password_looking_like_api_key_but_disabled_still_tries_password(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=("alice", "x" * 40, None)), \
             patch.object(auth_utils, "API_KEY_AUTH_ENABLED", False), \
             patch.object(
                auth_utils, "get_user_token",
                new=AsyncMock(return_value=("tok", "Alice"))):
            result = await auth_utils.verify_credentials(make_request())
        self.assertEqual(result, ("alice", "tok", "Alice"))

    async def test_no_credentials_returns_none_tuple(self):
        from tests.test_application import make_request
        with patch.object(
                auth_utils, "get_credentials_from_request",
                return_value=(None, None, None)):
            result = await auth_utils.verify_credentials(make_request())
        self.assertEqual(result, (None, None, None))


class GetAuthenticatedUserTests(unittest.IsolatedAsyncioTestCase):
    """Verify the FastAPI dependency wrapper around verify_credentials."""

    async def test_returns_credentials_on_success(self):
        with patch.object(
                auth_utils, "verify_credentials",
                new=AsyncMock(return_value=("alice", "tok", "Alice"))):
            result = await auth_utils.get_authenticated_user(MagicMock())
        self.assertEqual(result, ("alice", "tok", "Alice"))

    async def test_partial_credentials_return_none_tuple(self):
        with patch.object(
                auth_utils, "verify_credentials",
                new=AsyncMock(return_value=("alice", None, None))):
            result = await auth_utils.get_authenticated_user(MagicMock())
        self.assertEqual(result, (None, None, None))

    async def test_connection_failure_raises_503(self):
        from fastapi import HTTPException
        with patch.object(
                auth_utils, "verify_credentials",
                new=AsyncMock(
                    side_effect=AuthenticationError(
                        "Error connecting to Audiobookshelf: Cannot connect to host x"))):
            with self.assertRaises(HTTPException) as raised:
                await auth_utils.get_authenticated_user(MagicMock())
        self.assertEqual(raised.exception.status_code, 503)

    async def test_regular_auth_failure_raises_401(self):
        from fastapi import HTTPException
        with patch.object(
                auth_utils, "verify_credentials",
                new=AsyncMock(side_effect=AuthenticationError("bad login"))):
            with self.assertRaises(HTTPException) as raised:
                await auth_utils.get_authenticated_user(MagicMock())
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("WWW-Authenticate", raised.exception.headers)


class RequireAuthAndTokenLookupTests(unittest.IsolatedAsyncioTestCase):
    """Verify require_auth's success path and get_token_for_username."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    async def test_require_auth_returns_credentials_when_present(self):
        with patch.object(
                auth_utils, "get_authenticated_user",
                new=AsyncMock(return_value=("alice", "tok", "Alice"))):
            result = await auth_utils.require_auth(MagicMock())
        self.assertEqual(result, ("alice", "tok", "Alice"))

    def test_get_token_for_username_present_and_absent(self):
        auth_utils.TOKEN_CACHE["alice"] = ("tok", "Alice")
        self.assertEqual(auth_utils.get_token_for_username("alice"), "tok")
        self.assertIsNone(auth_utils.get_token_for_username("nobody"))


# ---------------------------------------------------------------------------
# cache_utils
# ---------------------------------------------------------------------------

class CachePersistenceTests(unittest.TestCase):
    """Verify disk persistence load/save behavior."""

    def setUp(self):
        cache_utils._cache.clear()
        cache_utils._save_state["last_save_time"] = 0.0

    def tearDown(self):
        cache_utils._cache.clear()
        cache_utils._save_state["last_save_time"] = 0.0

    def test_load_disabled_persistence_is_a_no_op(self):
        with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", False):
            cache_utils.load_cache_from_disk()
        self.assertEqual(cache_utils._cache, {})

    def test_load_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = str(Path(tmp_dir) / "does-not-exist.pkl")
            with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
                 patch.object(cache_utils, "CACHE_FILE_PATH", missing_path):
                cache_utils.load_cache_from_disk()
        self.assertEqual(cache_utils._cache, {})

    def test_load_reads_existing_pickle_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "cache.pkl"
            sample = {"key1": (time.time(), {"value": 1})}
            with cache_path.open("wb") as f:
                pickle.dump(sample, f)
            with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
                 patch.object(cache_utils, "CACHE_FILE_PATH", str(cache_path)):
                cache_utils.load_cache_from_disk()
        self.assertEqual(cache_utils._cache["key1"][1], {"value": 1})

    def test_load_corrupt_file_clears_cache(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "corrupt.pkl"
            cache_path.write_bytes(b"not a pickle file")
            cache_utils._cache["stale"] = (time.time(), {"value": "old"})
            with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
                 patch.object(cache_utils, "CACHE_FILE_PATH", str(cache_path)):
                cache_utils.load_cache_from_disk()
        self.assertEqual(cache_utils._cache, {})

    def test_save_disabled_persistence_is_a_no_op(self):
        with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", False):
            cache_utils.save_cache_to_disk()

    def test_save_respects_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "cache.pkl"
            cache_utils._save_state["last_save_time"] = time.time()
            with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
                 patch.object(cache_utils, "CACHE_FILE_PATH", str(cache_path)), \
                 patch.object(cache_utils, "CACHE_SAVE_INTERVAL", 300):
                cache_utils.save_cache_to_disk()
            self.assertFalse(cache_path.exists())

    def test_save_writes_file_and_prunes_expired_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "cache.pkl"
            cache_utils._cache["expired"] = (time.time() - 1000, {"value": "old"})
            cache_utils._cache["fresh"] = (time.time(), {"value": "new"})
            with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
                 patch.object(cache_utils, "CACHE_FILE_PATH", str(cache_path)), \
                 patch.object(cache_utils, "CACHE_SAVE_INTERVAL", 0), \
                 patch.object(cache_utils, "DEFAULT_CACHE_EXPIRY", 10):
                cache_utils.save_cache_to_disk()
            self.assertTrue(cache_path.exists())
        self.assertNotIn("expired", cache_utils._cache)
        self.assertIn("fresh", cache_utils._cache)


class CacheCoreTests(unittest.TestCase):
    """Verify get_cache, cache_set background save, and clear_cache."""

    def setUp(self):
        cache_utils._cache.clear()
        cache_utils._save_state["last_save_time"] = 0.0

    def tearDown(self):
        cache_utils._cache.clear()
        cache_utils._save_state["last_save_time"] = 0.0

    def test_get_cache_returns_the_live_dict(self):
        self.assertIs(cache_utils.get_cache(), cache_utils._cache)

    def test_cache_set_starts_background_save_when_due(self):
        with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
             patch.object(cache_utils, "CACHE_SAVE_INTERVAL", 0), \
             patch.object(cache_utils.threading, "Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            cache_utils.cache_set("key", {"value": 1})
        mock_thread_cls.assert_called_once()
        mock_thread.start.assert_called_once()

    def test_clear_cache_reports_count_and_triggers_save(self):
        cache_utils._cache["a"] = (time.time(), 1)
        cache_utils._cache["b"] = (time.time(), 2)
        with patch.object(cache_utils, "CACHE_PERSISTENCE_ENABLED", True), \
             patch.object(cache_utils, "save_cache_to_disk") as mock_save:
            count = cache_utils.clear_cache()
        self.assertEqual(count, 2)
        self.assertEqual(cache_utils._cache, {})
        mock_save.assert_called_once()


class CachedHelperFunctionsTests(unittest.IsolatedAsyncioTestCase):
    """Verify the shared caching helpers used by feed generators."""

    def setUp(self):
        cache_utils._cache.clear()

    def tearDown(self):
        cache_utils._cache.clear()

    async def test_get_cached_library_items_cache_hit_skips_fetch(self):
        fetch = AsyncMock()
        cache_key = cache_utils._create_cache_key("/library-items-all/lib-1", None, "alice")
        cache_utils.cache_set(cache_key, ["cached-item"])
        result = await cache_utils.get_cached_library_items(
            fetch, lambda data: data, "alice", "lib-1")
        self.assertEqual(result, ["cached-item"])
        fetch.assert_not_awaited()

    async def test_get_cached_library_items_cache_miss_fetches_and_caches(self):
        fetch = AsyncMock(return_value={"results": [{"id": "1"}]})
        result = await cache_utils.get_cached_library_items(
            fetch, lambda data: data["results"], "alice", "lib-1")
        self.assertEqual(result, [{"id": "1"}])
        fetch.assert_awaited_once()

    async def test_get_cached_library_items_bypass_cache_forces_fetch(self):
        cache_key = cache_utils._create_cache_key("/library-items-all/lib-1", None, "alice")
        cache_utils.cache_set(cache_key, ["stale"])
        fetch = AsyncMock(return_value={"results": ["fresh"]})
        result = await cache_utils.get_cached_library_items(
            fetch, lambda data: data["results"], "alice", "lib-1", bypass_cache=True)
        self.assertEqual(result, ["fresh"])
        fetch.assert_awaited_once()

    async def test_get_cached_search_results_cache_hit_and_miss(self):
        fetch = AsyncMock(return_value={"book": []})
        result = await cache_utils.get_cached_search_results(
            fetch, "alice", "lib-1", "dune")
        self.assertEqual(result, {"book": []})
        fetch.assert_awaited_once()

        fetch.reset_mock()
        result2 = await cache_utils.get_cached_search_results(
            fetch, "alice", "lib-1", "dune")
        self.assertEqual(result2, {"book": []})
        fetch.assert_not_awaited()

    async def test_get_cached_series_details_found_and_cached(self):
        fetch = AsyncMock(return_value={"results": [{"id": "s1", "name": "Series One"}]})
        result = await cache_utils.get_cached_series_details(fetch, "alice", "lib-1", "s1")
        self.assertEqual(result["name"], "Series One")

        fetch.reset_mock()
        cached_result = await cache_utils.get_cached_series_details(
            fetch, "alice", "lib-1", "s1")
        self.assertEqual(cached_result["name"], "Series One")
        fetch.assert_not_awaited()

    async def test_get_cached_series_details_not_found_returns_none(self):
        fetch = AsyncMock(return_value={"results": []})
        result = await cache_utils.get_cached_series_details(fetch, "alice", "lib-1", "missing")
        self.assertIsNone(result)

    async def test_get_cached_series_details_swallows_fetch_error(self):
        fetch = AsyncMock(side_effect=RuntimeError("boom"))
        result = await cache_utils.get_cached_series_details(fetch, "alice", "lib-1", "s1")
        self.assertIsNone(result)

    def test_count_authors_with_ebooks_counts_only_ebook_items(self):
        items = [
            {"media": {"ebookFile": {"ino": "1"}, "metadata": {"authorName": "A"}}},
            {"media": {"ebookFormat": "epub", "metadata": {"authorName": "A"}}},
            {"media": {"metadata": {"authorName": "B"}}},
            {"media": {"ebookFormat": "epub", "metadata": {}}},
        ]
        result = cache_utils._count_authors_with_ebooks(items)
        self.assertEqual(result["A"]["ebook_count"], 2)
        self.assertNotIn("B", result)

    async def test_enhance_authors_with_details_updates_in_place(self):
        fetch = AsyncMock(return_value={"authors": [
            {"name": "A", "id": "author-1", "imagePath": "/img"}]})
        authors = {"A": {"name": "A", "ebook_count": 1, "id": None, "imagePath": None}}
        ok = await cache_utils._enhance_authors_with_details(
            fetch, "lib-1", "alice", "tok", authors)
        self.assertTrue(ok)
        self.assertEqual(authors["A"]["id"], "author-1")

    async def test_enhance_authors_with_details_missing_key_returns_false(self):
        fetch = AsyncMock(return_value={})
        ok = await cache_utils._enhance_authors_with_details(
            fetch, "lib-1", "alice", "tok", {})
        self.assertFalse(ok)

    async def test_get_cached_author_details_empty_library_returns_empty_list(self):
        fetch = AsyncMock(return_value={"results": []})
        result = await cache_utils.get_cached_author_details(
            fetch, lambda data: data["results"], "alice", "lib-1")
        self.assertEqual(result, [])

    async def test_get_cached_author_details_full_flow_caches_result(self):
        async def fetch(endpoint, params=None, username=None, token=None):
            if endpoint == "/libraries/lib-1/items":
                return {"results": [
                    {"media": {"ebookFormat": "epub", "metadata": {"authorName": "A"}}}]}
            if endpoint == "/libraries/lib-1/authors":
                return {"authors": [{"name": "A", "id": "author-1", "imagePath": "/img"}]}
            raise AssertionError(f"unexpected endpoint {endpoint}")

        result = await cache_utils.get_cached_author_details(
            fetch, lambda data: data["results"], "alice", "lib-1")
        self.assertEqual(result[0]["id"], "author-1")

        cache_key = cache_utils._create_cache_key(
            "/authors-with-ebooks/lib-1", None, "alice")
        self.assertIsNotNone(cache_utils.cache_get(cache_key, 1800))

    async def test_get_cached_author_details_enhance_failure_returns_partial(self):
        async def fetch(endpoint, params=None, username=None, token=None):
            if endpoint == "/libraries/lib-1/items":
                return {"results": [
                    {"media": {"ebookFormat": "epub", "metadata": {"authorName": "A"}}}]}
            return {}

        result = await cache_utils.get_cached_author_details(
            fetch, lambda data: data["results"], "alice", "lib-1")
        self.assertEqual(result[0]["name"], "A")
        self.assertIsNone(result[0]["id"])

    async def test_get_cached_series_items_cache_hit_and_miss(self):
        fetch = AsyncMock(return_value={"results": [{"id": "b1"}]})
        result = await cache_utils.get_cached_series_items(
            fetch, lambda data: data["results"], "alice", "lib-1", "s1")
        self.assertEqual(result, [{"id": "b1"}])

        fetch.reset_mock()
        cached = await cache_utils.get_cached_series_items(
            fetch, lambda data: data["results"], "alice", "lib-1", "s1")
        self.assertEqual(cached, [{"id": "b1"}])
        fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# api.client
# ---------------------------------------------------------------------------

class ResolveAuthTokenTests(unittest.TestCase):
    """Verify token resolution precedence and error handling."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    def test_explicit_token_is_used_unchanged(self):
        params = {}
        with patch.object(api_client, "AUTH_ENABLED", True):
            result = api_client._resolve_auth_token("alice", "given-token", params)
        self.assertEqual(result, "given-token")

    def test_token_param_is_popped_and_used(self):
        params = {"token": "from-params"}
        with patch.object(api_client, "AUTH_ENABLED", True):
            result = api_client._resolve_auth_token("alice", None, params)
        self.assertEqual(result, "from-params")
        self.assertNotIn("token", params)

    def test_api_key_param_is_popped_and_used(self):
        params = {"api_key": "from-params"}
        with patch.object(api_client, "AUTH_ENABLED", True):
            result = api_client._resolve_auth_token("alice", None, params)
        self.assertEqual(result, "from-params")
        self.assertNotIn("api_key", params)

    def test_uses_token_cache_when_no_params(self):
        auth_utils.TOKEN_CACHE["alice"] = ("cached-token", "Alice")
        with patch.object(api_client, "AUTH_ENABLED", True):
            result = api_client._resolve_auth_token("alice", None, {})
        self.assertEqual(result, "cached-token")

    def test_missing_token_raises_when_auth_enabled(self):
        with patch.object(api_client, "AUTH_ENABLED", True):
            with self.assertRaises(AuthenticationError):
                api_client._resolve_auth_token("alice", None, {})

    def test_auth_disabled_returns_none_even_with_token(self):
        with patch.object(api_client, "AUTH_ENABLED", False):
            result = api_client._resolve_auth_token("alice", "given-token", {})
        self.assertIsNone(result)

    def test_no_token_skips_cache_lookup_when_auth_disabled(self):
        # With AUTH_ENABLED False, the token-cache branch must not even be
        # consulted; a stale cache entry should not leak through as a token.
        auth_utils.TOKEN_CACHE["alice"] = ("stale-token", "Alice")
        with patch.object(api_client, "AUTH_ENABLED", False):
            result = api_client._resolve_auth_token("alice", None, {})
        self.assertIsNone(result)


class BuildAuthHeadersAndExpiryTests(unittest.TestCase):
    """Verify header construction and cache-expiry endpoint mapping."""

    def test_build_headers_with_token(self):
        headers = api_client._build_auth_headers("tok", "/items/1")
        self.assertEqual(headers, {"Authorization": "Bearer tok"})

    def test_build_headers_without_token(self):
        headers = api_client._build_auth_headers(None, "/items/1")
        self.assertEqual(headers, {})

    def test_cache_expiry_matches_known_endpoint_categories(self):
        self.assertEqual(
            api_client._get_cache_expiry_for_endpoint("/libraries/1/items/1"),
            api_client.LIBRARY_ITEMS_CACHE_EXPIRY)
        self.assertEqual(
            api_client._get_cache_expiry_for_endpoint("/some/authors"),
            api_client.AUTHORS_CACHE_EXPIRY)

    def test_cache_expiry_falls_back_to_default(self):
        self.assertEqual(
            api_client._get_cache_expiry_for_endpoint("/unmapped/endpoint"),
            api_client.DEFAULT_CACHE_EXPIRY)


class HandleResponseErrorTests(unittest.TestCase):
    """Verify status-code-driven exception mapping and cache invalidation."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    def test_401_raises_authentication_error_and_invalidates_cache(self):
        auth_utils.TOKEN_CACHE["alice"] = ("tok", "Alice")
        error = make_response_error(401, "unauthorized")
        with self.assertRaises(AuthenticationError):
            api_client._handle_response_error(error, "tok", "alice", "http://x")
        self.assertNotIn("alice", auth_utils.TOKEN_CACHE)

    def test_500_raises_api_client_error(self):
        from opds_abs.utils.error_utils import APIClientError
        error = make_response_error(500, "server error")
        with self.assertRaises(APIClientError):
            api_client._handle_response_error(error, None, None, "http://x")


class FallbackCachedDataTests(unittest.TestCase):
    """Verify the expired-cache fallback used when the server is unreachable."""

    def setUp(self):
        cache_utils._cache.clear()

    def tearDown(self):
        cache_utils._cache.clear()

    def test_bypass_cache_returns_none(self):
        result = api_client._fallback_cached_data(
            {"bypass_cache": True, "cache_key": "k", "cache_expiry": 10, "endpoint": "/x"})
        self.assertIsNone(result)

    def test_expired_cache_is_returned_as_fallback(self):
        cache_utils._cache["k"] = (time.time() - 1000, {"value": "old"})
        result = api_client._fallback_cached_data(
            {"bypass_cache": False, "cache_key": "k", "cache_expiry": 10, "endpoint": "/x"})
        self.assertEqual(result, {"value": "old"})

    def test_no_cache_entry_returns_none(self):
        result = api_client._fallback_cached_data(
            {"bypass_cache": False, "cache_key": "missing", "cache_expiry": 10, "endpoint": "/x"})
        self.assertIsNone(result)


class RaiseConnectionErrorTests(unittest.TestCase):
    """Verify the unreachable-server error message."""

    def test_raises_api_client_error_with_host_info(self):
        from opds_abs.utils.error_utils import APIClientError
        with self.assertRaises(APIClientError) as raised:
            api_client._raise_connection_error(
                make_connector_error(), {"endpoint": "/items/1"})
        self.assertIn("Cannot connect to Audiobookshelf server", str(raised.exception))


class FetchViaHttpTests(unittest.IsolatedAsyncioTestCase):
    """Verify fetch_from_api's network error handling paths."""

    def setUp(self):
        cache_utils._cache.clear()
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        cache_utils._cache.clear()
        auth_utils.TOKEN_CACHE.clear()

    async def test_timeout_raises_api_client_error(self):
        from opds_abs.utils.error_utils import APIClientError
        session = ScriptedSession(get_results=[asyncio.TimeoutError()])
        with patch.object(api_client.aiohttp, "ClientSession", scripted_session_factory(session)), \
             patch.object(api_client, "AUTH_ENABLED", False):
            with self.assertRaises(APIClientError) as raised:
                await api_client.fetch_from_api("/items/1", bypass_cache=True)
        self.assertIn("not responding", str(raised.exception))

    async def test_response_error_is_wrapped_as_api_client_error(self):
        # _handle_response_error raises AuthenticationError, but it's raised
        # from inside the outer try's `async with` block, so the outer
        # `except Exception` re-wraps it as an APIClientError.
        from opds_abs.utils.error_utils import APIClientError

        response = FakeAsyncResponse(401)
        session = ScriptedSession(get_results=[response])
        with patch.object(api_client.aiohttp, "ClientSession", scripted_session_factory(session)), \
             patch.object(api_client, "AUTH_ENABLED", False):
            with self.assertRaises(APIClientError):
                await api_client.fetch_from_api("/items/1", bypass_cache=True)

    async def test_fetch_via_http_falls_back_to_expired_cache_on_connector_error(self):
        cache_key = "series-cache-key"
        cache_utils._cache[cache_key] = (time.time() - 100000, {"cached": True})
        session = ScriptedSession(get_results=[make_connector_error()])
        request_ctx = {
            "endpoint": "/items/1",
            "url": "http://localhost/api/items/1",
            "params": {},
            "headers": {},
            "token": None,
            "username": None,
            "cache_key": cache_key,
            "cache_expiry": 10,
            "bypass_cache": False,
        }
        with patch.object(api_client.aiohttp, "ClientSession", scripted_session_factory(session)):
            result = await api_client._fetch_via_http(request_ctx)
        self.assertEqual(result, {"cached": True})

    async def test_connector_error_without_cache_raises(self):
        from opds_abs.utils.error_utils import APIClientError
        session = ScriptedSession(get_results=[make_connector_error()])
        with patch.object(api_client.aiohttp, "ClientSession", scripted_session_factory(session)), \
             patch.object(api_client, "AUTH_ENABLED", False):
            with self.assertRaises(APIClientError):
                await api_client.fetch_from_api("/items/1", bypass_cache=True)

    async def test_unexpected_error_is_wrapped(self):
        from opds_abs.utils.error_utils import APIClientError
        session = ScriptedSession(get_results=[ValueError("weird")])
        with patch.object(api_client.aiohttp, "ClientSession", scripted_session_factory(session)), \
             patch.object(api_client, "AUTH_ENABLED", False):
            with self.assertRaises(APIClientError):
                await api_client.fetch_from_api("/items/1", bypass_cache=True)

    async def test_fresh_cache_entry_is_returned_without_a_network_call(self):
        endpoint = "/items/1"
        params = {}
        username = "alice"
        cache_key = cache_utils._create_cache_key(endpoint, params, username)
        cache_utils.cache_set(cache_key, {"cached": True})

        # No ClientSession is patched in, so a network call here would error
        # instead of silently succeeding - proving the cache hit short-circuits it.
        with patch.object(api_client, "AUTH_ENABLED", False):
            result = await api_client.fetch_from_api(
                endpoint, params, username=username, bypass_cache=False)
        self.assertEqual(result, {"cached": True})


class GetDownloadUrlsAndInvalidateCacheTests(unittest.IsolatedAsyncioTestCase):
    """Verify download URL extraction failure handling and cache invalidation."""

    def setUp(self):
        cache_utils._cache.clear()
        api_client._cache.clear()

    def tearDown(self):
        cache_utils._cache.clear()
        api_client._cache.clear()

    async def test_get_download_urls_returns_empty_list_on_error(self):
        with patch.object(
                api_client, "fetch_from_api",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await api_client.get_download_urls_from_item("book-1", username="alice")
        self.assertEqual(result, [])

    async def test_get_download_urls_uses_cached_token_when_missing(self):
        with patch.object(
                api_client, "fetch_from_api",
                new=AsyncMock(return_value={"libraryFiles": []})) as fetch, \
             patch.object(api_client, "get_token_for_username", return_value="cached-tok"):
            await api_client.get_download_urls_from_item("book-1", username="alice")
        self.assertEqual(fetch.call_args.kwargs["token"], "cached-tok")

    def test_invalidate_cache_removes_entry_and_reports_success(self):
        cache_key = api_client._create_cache_key("/items/1", None, "alice")
        api_client._cache[cache_key] = (time.time(), {"data": 1})
        self.assertTrue(api_client.invalidate_cache("/items/1", None, "alice"))
        self.assertNotIn(cache_key, api_client._cache)

    def test_invalidate_cache_missing_entry_returns_false(self):
        self.assertFalse(api_client.invalidate_cache("/items/missing"))

    def test_invalidate_cache_without_endpoint_returns_false(self):
        self.assertFalse(api_client.invalidate_cache())


if __name__ == "__main__":
    unittest.main()
