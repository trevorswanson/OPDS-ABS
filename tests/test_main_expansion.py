"""Expanded regression coverage for opds_abs.main routes and helpers.

See ``tests/test_application.py`` for the original suite (which already
covers navigation/series-item redirects, admin cache basics, and the
dedicated exception handlers) and the other ``tests/test_*_expansion.py``
files for auth/cache/client/feed coverage added alongside this file.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from fastapi.testclient import TestClient

from opds_abs import main
from tests.test_coverage_expansion import (
    ScriptedSession,
    make_connector_error,
    make_response_error,
    scripted_session_factory,
)


def override_auth(username="Bob", token="tok", display_name="Bob"):
    """Install a get_authenticated_user override returning fixed credentials."""
    main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
        username, token, display_name)


class IndexPageTests(unittest.TestCase):
    """Verify the index page renders and reports render failures."""

    def test_renders_successfully(self):
        with TestClient(main.app) as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_render_failure_returns_500(self):
        with patch.object(
                main.templates, "TemplateResponse", side_effect=RuntimeError("boom")):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get("/")
        self.assertEqual(response.status_code, 500)


class SearchXmlRouteTests(unittest.TestCase):
    """Verify the search.xml template route's redirect and render paths."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects_to_display_name(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get(
                    "/opds/bob/libraries/lib-1/search.xml", params={"q": "dune"})
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"], "/opds/Bob/libraries/lib-1/search.xml")

    def test_renders_template_for_matching_user(self):
        override_auth(username="Bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app) as client:
                response = client.get(
                    "/opds/Bob/libraries/lib-1/search.xml", params={"q": "dune"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("token=tok", response.text)

    def test_render_failure_returns_500(self):
        override_auth(username="Bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(
                main.templates, "TemplateResponse", side_effect=RuntimeError("boom")):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get("/opds/Bob/libraries/lib-1/search.xml")
        self.assertEqual(response.status_code, 500)


class OpdsRootUsernameRouteTests(unittest.TestCase):
    """Verify GET /opds/{username} redirect and success paths."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects_to_display_name(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get("/opds/bob")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/opds/Bob")

    def test_matching_user_returns_root_feed(self):
        override_auth(username="Bob", display_name="Bob")
        root_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main.library_feed, "generate_root_feed", new=root_mock):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob")
        self.assertEqual(response.status_code, 200)
        root_mock.assert_awaited_once_with("Bob", token="tok")

    def test_unexpected_error_returns_generic_error_response(self):
        override_auth(username="Bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(
                main.library_feed, "generate_root_feed",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob")
        self.assertEqual(response.status_code, 500)


class OpdsSearchRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/search."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects_preserving_query(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get(
                    "/opds/bob/libraries/lib-1/search", params={"q": "dune"})
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"], "/opds/Bob/libraries/lib-1/search?q=dune")

    def test_matching_user_returns_search_feed(self):
        override_auth(username="Bob", display_name="Bob")
        search_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main.search_feed, "generate_search_feed", new=search_mock):
            with TestClient(main.app) as client:
                response = client.get(
                    "/opds/Bob/libraries/lib-1/search", params={"q": "dune"})
        self.assertEqual(response.status_code, 200)
        search_mock.assert_awaited_once_with("Bob", "lib-1", {"q": "dune"}, token="tok")


class OpdsLibraryRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/items."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects_preserving_query(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get(
                    "/opds/bob/libraries/lib-1/items", params={"start_index": "5"})
        self.assertEqual(response.status_code, 307)
        self.assertIn("start_index=5", response.headers["location"])

    def test_matching_user_returns_items_feed(self):
        override_auth(username="Bob", display_name="Bob")
        items_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main.library_feed, "generate_library_items_feed", new=items_mock):
            with TestClient(main.app) as client:
                response = client.get(
                    "/opds/Bob/libraries/lib-1/items", params={"start_index": "5"})
        self.assertEqual(response.status_code, 200)
        items_mock.assert_awaited_once_with(
            "Bob", "lib-1", {"start_index": "5"}, token="tok")


class OpdsSeriesRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/series."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get("/opds/bob/libraries/lib-1/series")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"], "/opds/Bob/libraries/lib-1/series")

    def test_matching_user_returns_series_feed(self):
        override_auth(username="Bob", display_name="Bob")
        series_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main.series_feed, "generate_series_feed", new=series_mock):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob/libraries/lib-1/series")
        self.assertEqual(response.status_code, 200)
        series_mock.assert_awaited_once_with("Bob", "lib-1", token="tok")


class OpdsCollectionsRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/collections."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get("/opds/bob/libraries/lib-1/collections")
        self.assertEqual(response.status_code, 307)

    def test_matching_user_returns_collections_feed(self):
        override_auth(username="Bob", display_name="Bob")
        collections_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(
                main.collection_feed, "generate_collections_feed", new=collections_mock):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob/libraries/lib-1/collections")
        self.assertEqual(response.status_code, 200)
        collections_mock.assert_awaited_once_with("Bob", "lib-1", token="tok")


class OpdsCollectionItemsRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/collections/{collection_id}."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get("/opds/bob/libraries/lib-1/collections/c1")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"], "/opds/Bob/libraries/lib-1/collections/c1")

    def test_matching_user_returns_collection_items_feed(self):
        override_auth(username="Bob", display_name="Bob")
        items_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(
                main.collection_feed, "generate_collection_items_feed", new=items_mock):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob/libraries/lib-1/collections/c1")
        self.assertEqual(response.status_code, 200)
        items_mock.assert_awaited_once_with("Bob", "lib-1", "c1", token="tok")


class OpdsAuthorsRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/authors."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get("/opds/bob/libraries/lib-1/authors")
        self.assertEqual(response.status_code, 307)

    def test_matching_user_returns_authors_feed(self):
        override_auth(username="Bob", display_name="Bob")
        authors_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main.author_feed, "generate_authors_feed", new=authors_mock):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob/libraries/lib-1/authors")
        self.assertEqual(response.status_code, 200)
        authors_mock.assert_awaited_once_with("Bob", "lib-1", token="tok")


class OpdsAuthorItemsRouteTests(unittest.TestCase):
    """Verify GET /opds/{username}/libraries/{library_id}/authors/{author_id}."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_mismatched_user_redirects(self):
        override_auth(username="bob", display_name="Bob")
        with patch.object(main, "AUTH_ENABLED", True):
            with TestClient(main.app, follow_redirects=False) as client:
                response = client.get("/opds/bob/libraries/lib-1/authors/a1")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"], "/opds/Bob/libraries/lib-1/authors/a1")

    def test_matching_user_returns_author_items_feed(self):
        override_auth(username="Bob", display_name="Bob")
        items_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main.author_feed, "generate_author_items_feed", new=items_mock):
            with TestClient(main.app) as client:
                response = client.get("/opds/Bob/libraries/lib-1/authors/a1")
        self.assertEqual(response.status_code, 200)
        items_mock.assert_awaited_once_with("Bob", "lib-1", "a1", token="tok")


class AdminCacheExceptionTests(unittest.TestCase):
    """Verify admin cache endpoints report failures as errors."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_cache_stats_failure_returns_500(self):
        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        with patch.object(main, "get_cache", side_effect=RuntimeError("boom")):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get("/admin/cache/stats")
        self.assertEqual(response.status_code, 500)

    def test_cache_clear_failure_returns_500(self):
        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        with patch.object(main, "clear_cache", side_effect=RuntimeError("boom")):
            with TestClient(main.app) as client:
                response = client.post("/admin/cache/clear")
        self.assertEqual(response.status_code, 500)

    def test_cache_invalidate_requires_endpoint_param(self):
        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        with TestClient(main.app) as client:
            response = client.post("/admin/cache/invalidate", params={"endpoint": ""})
        self.assertEqual(response.status_code, 404)

    def test_cache_invalidate_not_found_returns_404(self):
        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        with patch.object(main, "invalidate_cache", return_value=False):
            with TestClient(main.app) as client:
                response = client.post(
                    "/admin/cache/invalidate", params={"endpoint": "/items/1"})
        self.assertEqual(response.status_code, 404)

    def test_cache_invalidate_success(self):
        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        with patch.object(main, "invalidate_cache", return_value=True):
            with TestClient(main.app) as client:
                response = client.post(
                    "/admin/cache/invalidate", params={"endpoint": "/items/1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalidated cache", response.json()["message"])

    def test_cache_invalidate_unexpected_error_returns_500(self):
        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        with patch.object(main, "invalidate_cache", side_effect=RuntimeError("boom")):
            with TestClient(main.app) as client:
                response = client.post(
                    "/admin/cache/invalidate", params={"endpoint": "/items/1"})
        self.assertEqual(response.status_code, 500)


class ProxyAuthenticatedImageTests(unittest.IsolatedAsyncioTestCase):
    """Verify the shared image proxy helper's status handling."""

    async def test_success_returns_image_content(self):
        response_obj = MagicMock()
        response_obj.status = 200
        response_obj.headers = {"Content-Type": "image/png"}
        response_obj.raise_for_status = MagicMock()

        async def fake_read():
            return b"image-bytes"
        response_obj.read = fake_read

        class FakeGetCtx:
            async def __aenter__(self_inner):
                return response_obj

            async def __aexit__(self_inner, *exc):
                return None

        session = MagicMock()
        session.get = MagicMock(return_value=FakeGetCtx())

        class FakeSessionCtx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *exc):
                return None

        with patch.object(main.aiohttp, "ClientSession", return_value=FakeSessionCtx()):
            result = await main._proxy_authenticated_image("http://x/cover", "tok")
        self.assertEqual(result.body, b"image-bytes")

    async def test_401_upstream_raises_401(self):
        from fastapi import HTTPException as FastAPIHTTPException

        response_obj = MagicMock()
        response_obj.status = 401

        class FakeGetCtx:
            async def __aenter__(self_inner):
                return response_obj

            async def __aexit__(self_inner, *exc):
                return None

        session = MagicMock()
        session.get = MagicMock(return_value=FakeGetCtx())

        class FakeSessionCtx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *exc):
                return None

        with patch.object(main.aiohttp, "ClientSession", return_value=FakeSessionCtx()):
            with self.assertRaises(FastAPIHTTPException) as raised:
                await main._proxy_authenticated_image("http://x/cover", "tok")
        self.assertEqual(raised.exception.status_code, 401)

    async def test_404_upstream_raises_404(self):
        from fastapi import HTTPException as FastAPIHTTPException

        response_obj = MagicMock()
        response_obj.status = 404

        class FakeGetCtx:
            async def __aenter__(self_inner):
                return response_obj

            async def __aexit__(self_inner, *exc):
                return None

        session = MagicMock()
        session.get = MagicMock(return_value=FakeGetCtx())

        class FakeSessionCtx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *exc):
                return None

        with patch.object(main.aiohttp, "ClientSession", return_value=FakeSessionCtx()):
            with self.assertRaises(FastAPIHTTPException) as raised:
                await main._proxy_authenticated_image("http://x/cover", "tok")
        self.assertEqual(raised.exception.status_code, 404)

    async def test_client_error_raises_502(self):
        from fastapi import HTTPException as FastAPIHTTPException

        class FakeSessionCtx:
            async def __aenter__(self_inner):
                raise aiohttp.ClientError("boom")

            async def __aexit__(self_inner, *exc):
                return None

        with patch.object(main.aiohttp, "ClientSession", return_value=FakeSessionCtx()):
            with self.assertRaises(FastAPIHTTPException) as raised:
                await main._proxy_authenticated_image("http://x/cover", "tok")
        self.assertEqual(raised.exception.status_code, 502)


class ProxyAuthorImageRouteTests(unittest.TestCase):
    """Verify the author-image proxy route's auth requirement and success path."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_requires_token(self):
        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (None, None, None)
        with TestClient(main.app) as client:
            response = client.get("/opds/proxy/author-image/a1")
        self.assertEqual(response.status_code, 401)

    def test_success_proxies_image(self):
        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "alice", "tok", "Alice")

        async def fake_proxy(url, token):
            return main.Response(content=b"image", media_type="image/jpeg")

        with patch.object(main, "_proxy_authenticated_image", side_effect=fake_proxy) as proxy:
            with TestClient(main.app) as client:
                response = client.get("/opds/proxy/author-image/a1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/authors/a1/image", proxy.await_args.args[0])


class ProxyDownloadRouteTests(unittest.TestCase):
    """Verify the download proxy route's auth requirement, success, and failure paths."""

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_requires_token(self):
        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (None, None, None)
        with TestClient(main.app) as client:
            response = client.get("/opds/proxy/download/book-1/file/99")
        self.assertEqual(response.status_code, 401)

    def test_success_streams_file(self):
        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "alice", "tok", "Alice")

        async def fake_head_info(url, headers, item_id):
            return "application/epub+zip", {"Content-Disposition": 'attachment; filename="b.epub"'}

        async def fake_stream(url, headers):
            yield b"chunk1"
            yield b"chunk2"

        with patch.object(main, "_fetch_download_head_info", side_effect=fake_head_info), \
             patch.object(main, "_stream_download_file", fake_stream):
            with TestClient(main.app) as client:
                response = client.get("/opds/proxy/download/book-1/file/99")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"chunk1chunk2")

    def test_head_info_failure_returns_500(self):
        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "alice", "tok", "Alice")

        async def failing_head_info(url, headers, item_id):
            raise RuntimeError("boom")

        with patch.object(main, "_fetch_download_head_info", side_effect=failing_head_info):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get("/opds/proxy/download/book-1/file/99")
        self.assertEqual(response.status_code, 500)


class StreamDownloadFileTests(unittest.IsolatedAsyncioTestCase):
    """Verify _stream_download_file's success and error-translation paths."""

    async def test_success_yields_chunks(self):
        response_obj = MagicMock()
        response_obj.status = 200
        response_obj.raise_for_status = MagicMock()

        async def iter_any():
            yield b"a"
            yield b"b"
        response_obj.content.iter_any = iter_any

        session = ScriptedSession(get_results=[response_obj])
        # ScriptedSession.get() returns the object directly; make it usable
        # as an async context manager like a real aiohttp response.
        response_obj.__aenter__ = AsyncMock(return_value=response_obj)
        response_obj.__aexit__ = AsyncMock(return_value=None)

        with patch.object(main.aiohttp, "ClientSession", scripted_session_factory(session)):
            chunks = [chunk async for chunk in main._stream_download_file("http://x", {})]
        self.assertEqual(chunks, [b"a", b"b"])

    async def test_response_error_raised_as_http_exception(self):
        from fastapi import HTTPException as FastAPIHTTPException

        response_obj = MagicMock()
        response_obj.raise_for_status = MagicMock(side_effect=make_response_error(404, "missing"))
        response_obj.__aenter__ = AsyncMock(return_value=response_obj)
        response_obj.__aexit__ = AsyncMock(return_value=None)

        session = ScriptedSession(get_results=[response_obj])
        with patch.object(main.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(FastAPIHTTPException) as raised:
                async for _ in main._stream_download_file("http://x", {}):
                    pass
        self.assertEqual(raised.exception.status_code, 404)

    async def test_generic_error_raised_as_500(self):
        from fastapi import HTTPException as FastAPIHTTPException

        session = ScriptedSession(get_results=[ValueError("weird")])
        with patch.object(main.aiohttp, "ClientSession", scripted_session_factory(session)):
            with self.assertRaises(FastAPIHTTPException) as raised:
                async for _ in main._stream_download_file("http://x", {}):
                    pass
        self.assertEqual(raised.exception.status_code, 500)


class FetchDownloadHeadInfoTests(unittest.IsolatedAsyncioTestCase):
    """Verify _fetch_download_head_info's header extraction and fallback."""

    async def test_adds_default_content_disposition_when_missing(self):
        response_obj = MagicMock()
        response_obj.raise_for_status = MagicMock()
        response_obj.headers = {"Content-Type": "application/epub+zip"}
        response_obj.__aenter__ = AsyncMock(return_value=response_obj)
        response_obj.__aexit__ = AsyncMock(return_value=None)

        session = ScriptedSession()
        session.head = MagicMock(return_value=response_obj)

        with patch.object(main.aiohttp, "ClientSession", scripted_session_factory(session)):
            content_type, headers = await main._fetch_download_head_info(
                "http://x", {}, "book-1")
        self.assertEqual(content_type, "application/epub+zip")
        self.assertIn("Content-Disposition", headers)
        self.assertIn("book-1", headers["Content-Disposition"])

    async def test_preserves_existing_content_disposition(self):
        response_obj = MagicMock()
        response_obj.raise_for_status = MagicMock()
        response_obj.headers = {
            "Content-Type": "application/epub+zip",
            "Content-Disposition": 'attachment; filename="real.epub"',
        }
        response_obj.__aenter__ = AsyncMock(return_value=response_obj)
        response_obj.__aexit__ = AsyncMock(return_value=None)

        session = ScriptedSession()
        session.head = MagicMock(return_value=response_obj)

        with patch.object(main.aiohttp, "ClientSession", scripted_session_factory(session)):
            _content_type, headers = await main._fetch_download_head_info(
                "http://x", {}, "book-1")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="real.epub"')

    async def test_head_failure_falls_back_to_generic_headers(self):
        session = ScriptedSession()
        session.head = MagicMock(side_effect=make_connector_error())

        with patch.object(main.aiohttp, "ClientSession", scripted_session_factory(session)):
            content_type, headers = await main._fetch_download_head_info(
                "http://x", {}, "book-1")
        self.assertEqual(content_type, "application/octet-stream")
        self.assertEqual(headers, {})


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    """Verify the startup/shutdown lifespan logs and cache persistence hooks."""

    async def test_lifespan_with_persistence_and_pagination_disabled(self):
        with patch.object(main, "CACHE_PERSISTENCE_ENABLED", True), \
             patch.object(main, "API_KEY_AUTH_ENABLED", False), \
             patch.object(main, "PAGINATION_ENABLED", False), \
             patch.object(main, "load_cache_from_disk") as load_mock, \
             patch.object(main, "save_cache_to_disk") as save_mock:
            async with main.lifespan(main.app):
                pass
        load_mock.assert_called_once()
        save_mock.assert_called_once()

    async def test_lifespan_without_persistence(self):
        with patch.object(main, "CACHE_PERSISTENCE_ENABLED", False), \
             patch.object(main, "API_KEY_AUTH_ENABLED", True), \
             patch.object(main, "PAGINATION_ENABLED", True), \
             patch.object(main, "load_cache_from_disk") as load_mock, \
             patch.object(main, "save_cache_to_disk") as save_mock:
            async with main.lifespan(main.app):
                pass
        load_mock.assert_not_called()
        save_mock.assert_not_called()


class ExceptionHandlerContextTests(unittest.IsolatedAsyncioTestCase):
    """Verify handler context derivation when only a username is present."""

    async def test_authentication_error_handler_username_only_context(self):
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/opds/Bob"
        response = await main.authentication_error_handler(
            request, main.AuthenticationError("token expired"))
        self.assertIn(b"Authentication for user Bob", response.body)

    async def test_api_client_error_handler_no_username_uses_generic_context(self):
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/some/other/path"
        response = await main.api_client_error_handler(
            request, main.APIClientError("upstream down"))
        self.assertIn(b"GET /some/other/path", response.body)


if __name__ == "__main__":
    unittest.main()
