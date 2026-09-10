"""Application-level tests for OPDS feeds, utilities, and HTTP boundaries."""

import asyncio
import base64
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from lxml import etree
from starlette.requests import Request

from opds_abs.api import client as api_client
from opds_abs.core.feed_generator import BaseFeedGenerator
from opds_abs.feeds.navigation_feed import NavigationFeedGenerator
from opds_abs.utils import xml_utils
from opds_abs.utils import auth_utils, cache_utils
from opds_abs.utils.error_utils import (
    APIClientError,
    AuthenticationError,
    ResourceNotFoundError,
    convert_to_http_exception,
    handle_exception,
)


def make_request(path="/", headers=None, query_string=b""):
    """Create a Starlette request without opening a network socket."""
    encoded_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": encoded_headers,
        "server": ("testserver", 80),
        "scheme": "http",
        "client": ("testclient", 50000),
    }
    return Request(scope)


class XmlAndErrorTests(unittest.TestCase):
    """Verify XML conversion and standardized error responses."""

    def test_dict_to_xml_supports_nested_lists_without_mutating_input(self):
        data = {
            "entry": {
                "_attrs": {"id": "book-1"},
                "title": {"_text": "A book"},
                "link": [{"_attrs": {"href": "/one"}}, {"_attrs": {"href": "/two"}}],
            }
        }
        original = copy.deepcopy(data)
        root = etree.Element("feed")

        xml_utils.dict_to_xml(root, data)

        self.assertEqual(data, original)
        self.assertEqual(root.find("entry").get("id"), "book-1")
        self.assertEqual([link.get("href")
                         for link in root.findall("entry/link")], ["/one", "/two"])

    def test_error_helpers_return_expected_formats_and_statuses(self):
        xml_response = handle_exception(ResourceNotFoundError(
            "missing"), context="lookup", log_traceback=False)
        json_response = handle_exception(AuthenticationError(
            "bad login"), return_json=True, log_traceback=False)

        self.assertEqual(xml_response.status_code, 404)
        self.assertIn(b"<message>Resource not found</message>", xml_response.body)
        self.assertEqual(json_response.status_code, 401)
        self.assertEqual(json.loads(json_response.body)["message"], "Authentication failed")
        self.assertEqual(convert_to_http_exception(
            APIClientError("upstream"), status_code=504).status_code, 504)


class CacheTests(unittest.TestCase):
    """Verify deterministic, expiring, and decorated cache behavior."""

    def setUp(self):
        cache_utils._cache.clear()

    def tearDown(self):
        cache_utils._cache.clear()

    def test_cache_key_is_stable_for_parameter_order(self):
        first = cache_utils._create_cache_key("/items", {"b": 2, "a": 1}, "alice")
        second = cache_utils._create_cache_key("/items", {"a": 1, "b": 2}, "alice")
        self.assertEqual(first, second)
        self.assertNotEqual(first, cache_utils._create_cache_key("/items", {"a": 1}, "alice"))

    def test_cache_expires_and_can_be_read_for_fallback(self):
        cache_utils._cache["old"] = (time.time() - 100, {"value": 1})
        self.assertIsNone(cache_utils.cache_get("old", max_age=1))
        cache_utils._cache["old"] = (time.time() - 100, {"value": 1})
        self.assertEqual(cache_utils.cache_get("old", max_age=1, ignore_expiry=True), {"value": 1})

    def test_cached_decorator_avoids_second_call(self):
        calls = []

        @cache_utils.cached(expiry=60)
        async def get_value(value):
            calls.append(value)
            return {"value": value}

        self.assertEqual(asyncio.run(get_value("x")), {"value": "x"})
        self.assertEqual(asyncio.run(get_value("x")), {"value": "x"})
        self.assertEqual(calls, ["x"])


class AuthTests(unittest.IsolatedAsyncioTestCase):
    """Verify credential parsing, caching, and authentication boundaries."""

    def setUp(self):
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        auth_utils.TOKEN_CACHE.clear()

    def test_basic_bearer_and_query_credentials_are_parsed(self):
        basic = base64.b64encode(b"alice:secret").decode()
        self.assertEqual(
            auth_utils.get_credentials_from_request(
                make_request(headers={"Authorization": f"Basic {basic}"})),
            ("alice", "secret", None),
        )
        self.assertEqual(
            auth_utils.get_credentials_from_request(
                make_request(headers={"Authorization": "Bearer token"},
                             query_string=b"username=alice")
            ),
            ("alice", None, "token"),
        )
        self.assertEqual(
            auth_utils.get_credentials_from_request(
                make_request(query_string=b"token=key&username=alice")),
            ("alice", None, "key"),
        )

    async def test_cached_token_is_returned_without_authentication_call(self):
        auth_utils.TOKEN_CACHE["alice"] = ("token", "Alice")
        with patch.object(
                auth_utils, "authenticate_with_audiobookshelf",
                new_callable=AsyncMock) as authenticate:
            result = await auth_utils.get_user_token("alice", "ignored")
        self.assertEqual(result, ("token", "Alice"))
        authenticate.assert_not_awaited()

    async def test_missing_credentials_are_rejected(self):
        with self.assertRaises(AuthenticationError):
            await auth_utils.get_user_token("alice")

    async def test_authentication_disabled_skips_request_verification(self):
        with patch.object(auth_utils, "AUTH_ENABLED", False):
            self.assertEqual(
                await auth_utils.verify_credentials(make_request()),
                (None, None, None))

    async def test_require_auth_rejects_missing_credentials(self):
        with patch.object(
                auth_utils, "get_authenticated_user",
                new=AsyncMock(return_value=(None, None, None))):
            with self.assertRaises(HTTPException) as raised:
                await auth_utils.require_auth(make_request())
        self.assertEqual(raised.exception.status_code, 401)


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    """Verify API request construction, caching, and safe failures."""

    def setUp(self):
        api_client._cache.clear()
        auth_utils.TOKEN_CACHE.clear()

    def tearDown(self):
        api_client._cache.clear()
        auth_utils.TOKEN_CACHE.clear()

    async def test_fetch_uses_token_from_params_and_caches_response(self):
        class Response:
            status = 200

            def raise_for_status(self):
                return None

            async def json(self):
                return {"results": [1]}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Session:
            def __init__(self, *args, **kwargs):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        with patch.object(api_client.aiohttp, "ClientSession", Session), \
                patch.object(api_client, "AUTH_ENABLED", True):
            result = await api_client.fetch_from_api(
                "/items/1", {"token": "secret"},
                username="alice", bypass_cache=True)
        self.assertEqual(result, {"results": [1]})
        cached = api_client.cache_get(api_client._create_cache_key("/items/1", {}, "alice"))
        self.assertEqual(cached, {"results": [1]})

    async def test_fetch_requires_cached_token_when_authentication_enabled(self):
        with patch.object(api_client, "AUTH_ENABLED", True):
            with self.assertRaises(AuthenticationError):
                await api_client.fetch_from_api("/items/1", username="alice")

    async def test_download_url_extraction_keeps_only_ebook_files(self):
        payload = {"libraryFiles": [
            {"fileType": "ebook", "ino": "1", "metadata": {"filename": "book.epub"}},
            {"fileType": "audio", "ino": "2", "metadata": {"filename": "book.mp3"}},
        ]}
        with patch.object(api_client, "fetch_from_api", new=AsyncMock(return_value=payload)):
            result = await api_client.get_download_urls_from_item("book", token="token")
        self.assertEqual(result[0]["filename"], "book.epub")
        self.assertEqual(len(result), 1)


class FeedGeneratorTests(unittest.TestCase):
    """Verify feed metadata, filtering, pagination, and acquisition links."""

    def setUp(self):
        self.generator = BaseFeedGenerator()

    def test_filter_sort_extract_and_paginate(self):
        data = {"results": [
            {"id": "1", "media": {"ebookFormat": "epub"}},
            {"id": "2", "media": {}},
            {"id": "3", "media": {"ebookFormat": "pdf"}},
        ]}
        filtered = self.generator.filter_items(data)
        self.assertEqual([item["id"] for item in filtered], ["1", "3"])
        self.assertEqual([item["id"]
                         for item in self.generator.paginate_results(filtered, 2, 1)], ["3"])
        self.assertEqual(self.generator.extract_value({"a": {"b": 2}}, "a.b"), 2)
        self.assertIsNone(self.generator.extract_value({"a": {}}, "a.b.c"))
        self.assertEqual(self.generator.create_filter("series"), "c2VyaWVz")

    def test_book_feed_contains_mime_type_cover_and_download_links(self):
        feed = self.generator.create_base_feed()
        book = {"id": "book-1", "addedAt": 0, "media": {"metadata": {
            "title": "Title", "authorName": "Author", "genres": [], "description": "Desc"
        }, "ebookFormat": "pdf"}}
        self.generator.add_book_to_feed(feed, book, [{"ino": "99"}], token="secret")
        xml = etree.tostring(feed).decode()
        self.assertIn("application/pdf", xml)
        self.assertIn("/opds/proxy/cover/book-1", xml)
        self.assertIn("/opds/proxy/download/book-1/file/99", xml)

    def test_book_feed_series_filter_adds_series_entry(self):
        feed = self.generator.create_base_feed()
        book = {"id": "book-1", "addedAt": 0, "media": {"metadata": {
            "title": "Title", "authorName": "Author", "genres": [], "description": "Desc",
            "series": {"name": "The Series", "sequence": "2"},
        }, "ebookFormat": "epub"}}
        self.generator.add_book_to_feed(feed, book, [{"ino": "99"}], query_filter="series123")
        xml = etree.tostring(feed).decode()
        self.assertIn("The Series #2", xml)
        self.assertNotIn("secret", xml)

    def test_pagination_metadata_and_links_are_generated(self):
        feed = self.generator.create_base_feed()
        self.generator.add_pagination_metadata(feed, page=2, items_per_page=10, total_items=25)
        self.generator.add_pagination_links(feed, "alice/libraries/lib/items", 2, 10, 25)
        xml = etree.tostring(feed).decode()
        self.assertIn(">11</opensearch:startIndex>", xml)
        self.assertIn('rel="previous"', xml)
        self.assertIn('start_index=21', xml)


class PaginatePageItemsTests(unittest.TestCase):
    """Verify page-number based pagination's bounds-clamping and disabled mode."""

    def test_pagination_disabled_returns_all_items_unpaged(self):
        from opds_abs.core import feed_generator as feed_generator_module

        items = list(range(5))
        with patch.object(feed_generator_module, "PAGINATION_ENABLED", False):
            paged, page, total_pages, no_pagination = BaseFeedGenerator().paginate_page_items(
                items, page=1, per_page=2)
        self.assertEqual(paged, items)
        self.assertEqual(page, 1)
        self.assertEqual(total_pages, 1)
        self.assertTrue(no_pagination)

    def test_page_below_one_clamps_to_first_page(self):
        paged, page, _total_pages, _no_pagination = BaseFeedGenerator().paginate_page_items(
            list(range(10)), page=0, per_page=5)
        self.assertEqual(page, 1)
        self.assertEqual(paged, list(range(5)))

    def test_page_beyond_last_clamps_to_last_page(self):
        paged, page, total_pages, _no_pagination = BaseFeedGenerator().paginate_page_items(
            list(range(10)), page=99, per_page=5)
        self.assertEqual(page, total_pages)
        self.assertEqual(paged, list(range(5, 10)))


class AddPagedBooksToFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify batch ebook-file fetching pairs each result with the right book."""

    async def test_book_without_id_is_skipped_without_misaligning_others(self):
        from opds_abs.core import feed_generator as feed_generator_module

        generator = BaseFeedGenerator()
        book_a = {"id": "a"}
        book_missing_id = {"media": {}}
        book_b = {"id": "b"}

        async def fake_download_urls(book_id, username=None, token=None):
            del username, token
            return [{"ino": book_id}]

        with patch.object(
                feed_generator_module, "get_download_urls_from_item",
                new=AsyncMock(side_effect=fake_download_urls)), \
             patch.object(generator, "add_book_to_feed") as add_mock:
            await generator.add_paged_books_to_feed(
                "feed", [book_a, book_missing_id, book_b], "alice", "tok")

        self.assertEqual(add_mock.call_count, 2)
        add_mock.assert_any_call("feed", book_a, [{"ino": "a"}], "", "tok")
        add_mock.assert_any_call("feed", book_b, [{"ino": "b"}], "", "tok")


class FeedAndRouteTests(unittest.IsolatedAsyncioTestCase):
    """Verify specialized feed output and representative HTTP route behavior."""

    async def test_navigation_feed_contains_all_navigation_entries(self):
        response = await NavigationFeedGenerator().generate_navigation_feed(
            "alice", "lib", token="token")
        body = response.body.decode()
        self.assertEqual(response.media_type, "application/atom+xml")
        self.assertIn("Navigation", body)
        self.assertIn("token=token", body)

    def test_proxy_route_requires_authentication(self):
        from opds_abs import main

        with patch.object(
                main, "get_authenticated_user",
                new=AsyncMock(return_value=(None, None, None))):
            main.app.dependency_overrides[main.get_authenticated_user] = lambda: (None, None, None)
            try:
                with TestClient(main.app) as test_client:
                    response = test_client.get("/opds/proxy/cover/book-1")
            finally:
                main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_authenticated_cover_route_uses_proxy_target(self):
        from opds_abs import main

        async def fake_proxy(url, token):
            return main.Response(content=b"image", media_type="image/jpeg")

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "alice", "token", "Alice")
        try:
            with patch.object(main, "_proxy_authenticated_image", side_effect=fake_proxy) as proxy:
                with TestClient(main.app) as test_client:
                    response = test_client.get("/opds/proxy/cover/book-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200)
        proxy.assert_awaited_once()
        self.assertIn("/items/book-1/cover", proxy.await_args.args[0])

    def test_root_redirect_sends_mismatched_user_to_display_name(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "bob", "token", "Bob")
        try:
            with patch.object(main, "AUTH_ENABLED", True):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get("/opds")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/opds/Bob")

    def test_root_redirect_uses_anonymous_when_auth_disabled(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            None, None, None)
        try:
            with patch.object(main, "AUTH_ENABLED", False):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get("/opds")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/opds/anonymous")

    def test_root_redirect_requires_authentication(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            None, None, None)
        try:
            with patch.object(main, "AUTH_ENABLED", True):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get("/opds")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_nav_route_redirects_on_username_mismatch(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "bob", "token", "Bob")
        try:
            with patch.object(main, "AUTH_ENABLED", True):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get("/opds/bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/opds/Bob/libraries/lib-1")

    def test_nav_route_serves_feed_when_username_matches(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "Bob", "token", "Bob")
        nav_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        try:
            with patch.object(main, "AUTH_ENABLED", True), \
                 patch.object(main.navigation_feed, "generate_navigation_feed", new=nav_mock):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get("/opds/Bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200)
        nav_mock.assert_awaited_once_with("Bob", "lib-1", token="token")

    def test_search_route_preserves_query_params_on_redirect(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "bob", "token", "Bob")
        try:
            with patch.object(main, "AUTH_ENABLED", True):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get(
                        "/opds/bob/libraries/lib-1/search", params={"q": "dune"})
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"], "/opds/Bob/libraries/lib-1/search?q=dune")

    def test_series_items_route_serves_feed_for_matching_user(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "Bob", "token", "Bob")
        series_mock = AsyncMock(return_value=main.Response(
            content=b"<feed/>", media_type="application/atom+xml"))
        try:
            with patch.object(main, "AUTH_ENABLED", True), \
                 patch.object(main.series_feed, "generate_series_items_feed", new=series_mock):
                with TestClient(main.app, follow_redirects=False) as test_client:
                    response = test_client.get(
                        "/opds/Bob/libraries/lib-1/series/series-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200)
        series_mock.assert_awaited_once_with(
            "Bob", "lib-1", "series-1", token="token")

    def test_admin_cache_stats_reports_entries(self):
        from opds_abs import main

        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        try:
            with patch.object(
                    main, "get_cache",
                    return_value={"key12345": (time.time(), {"a": 1})}):
                with TestClient(main.app) as test_client:
                    response = test_client.get("/admin/cache/stats")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_entries"], 1)
        self.assertEqual(body["entries"][0]["key"], "key12345...")

    def test_admin_cache_clear_reports_count(self):
        from opds_abs import main

        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        try:
            with patch.object(main, "clear_cache", return_value=3):
                with TestClient(main.app) as test_client:
                    response = test_client.post("/admin/cache/clear")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Cleared 3 items from cache")

    def test_admin_cache_invalidate_requires_endpoint(self):
        from opds_abs import main

        main.app.dependency_overrides[main.require_auth] = lambda: ("bob", "token", "Bob")
        try:
            with TestClient(main.app) as test_client:
                response = test_client.post("/admin/cache/invalidate")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 404)

    def test_resource_not_found_error_returns_generic_xml_message(self):
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "Bob", "token", "Bob")
        root_mock = AsyncMock(side_effect=main.ResourceNotFoundError("book xyz missing"))
        try:
            with patch.object(main, "AUTH_ENABLED", True), \
                 patch.object(main.library_feed, "generate_root_feed", new=root_mock):
                with TestClient(main.app) as test_client:
                    response = test_client.get("/opds/Bob")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"<message>Resource not found</message>", response.content)

    def test_authentication_error_inside_route_returns_generic_401_xml(self):
        """Verify handle_exception (not the dedicated handler) handles this.

        AuthenticationError raised while building a feed is caught by the
        route's own try/except and rendered via handle_exception - it never
        reaches the dedicated authentication_error_handler (that only fires
        for exceptions raised outside the route body, e.g. in a dependency),
        so no WWW-Authenticate header is added here.
        """
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "Bob", "token", "Bob")
        nav_mock = AsyncMock(side_effect=main.AuthenticationError("token expired"))
        try:
            with patch.object(main, "AUTH_ENABLED", True), \
                 patch.object(main.navigation_feed, "generate_navigation_feed", new=nav_mock):
                with TestClient(main.app) as test_client:
                    response = test_client.get("/opds/Bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("WWW-Authenticate", response.headers)
        self.assertIn(b"<message>Authentication failed</message>", response.content)

    def test_api_client_error_inside_route_returns_generic_502_xml(self):
        """Verify handle_exception (not the dedicated handler) handles this.

        Same as above for APIClientError: caught by the route's own
        try/except (status_code=502 per the exception class), not the
        dedicated api_client_error_handler.
        """
        from opds_abs import main

        main.app.dependency_overrides[main.get_authenticated_user] = lambda: (
            "Bob", "token", "Bob")
        nav_mock = AsyncMock(side_effect=main.APIClientError("upstream unreachable"))
        try:
            with patch.object(main, "AUTH_ENABLED", True), \
                 patch.object(main.navigation_feed, "generate_navigation_feed", new=nav_mock):
                with TestClient(main.app) as test_client:
                    response = test_client.get("/opds/Bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 502)
        self.assertIn(
            b"<message>Error communicating with Audiobookshelf</message>", response.content)

    def test_authentication_error_handler_fires_outside_route_body(self):
        """Verify the dedicated authentication_error_handler fires here.

        AuthenticationError raised from a dependency (before the route
        body's try/except runs) reaches the dedicated app-level handler,
        which - unlike handle_exception - includes the specific message and
        a WWW-Authenticate header.
        """
        from opds_abs import main

        async def raise_authentication_error(_request=None):
            raise main.AuthenticationError("token expired")

        main.app.dependency_overrides[main.get_authenticated_user] = raise_authentication_error
        try:
            with TestClient(main.app) as test_client:
                response = test_client.get("/opds/Bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)
        self.assertIn(b"<message>token expired</message>", response.content)
        self.assertIn(b"Authentication for user Bob, library lib-1", response.content)

    def test_api_client_error_handler_fires_outside_route_body(self):
        """Verify the dedicated api_client_error_handler fires here.

        APIClientError raised from a dependency reaches the dedicated
        api_client_error_handler (503, specific message verbatim, path-
        derived context), rather than being caught by the route body.
        """
        from opds_abs import main

        async def raise_api_client_error(_request=None):
            raise main.APIClientError("upstream unreachable")

        main.app.dependency_overrides[main.get_authenticated_user] = raise_api_client_error
        try:
            with TestClient(main.app) as test_client:
                response = test_client.get("/opds/Bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"<message>upstream unreachable</message>", response.content)
        self.assertIn(b"Generating items feed for user Bob, library lib-1", response.content)

    def test_service_unavailable_from_auth_returns_503_xml(self):
        from opds_abs import main

        async def raise_service_unavailable(_request=None):
            raise HTTPException(
                status_code=503, detail="Audiobookshelf server is unavailable")

        main.app.dependency_overrides[main.get_authenticated_user] = raise_service_unavailable
        try:
            with TestClient(main.app) as test_client:
                response = test_client.get("/opds/Bob/libraries/lib-1")
        finally:
            main.app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Audiobookshelf server is unavailable", response.content)


class SpecializedFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify author, collection, series, search, and library contracts."""

    async def test_author_entry_uses_authenticated_image_proxy(self):
        from opds_abs.feeds.author_feed import AuthorFeedGenerator

        feed = AuthorFeedGenerator().create_base_feed()
        AuthorFeedGenerator().add_author_to_feed(
            "alice", "lib", feed,
            {"id": "author-1", "name": "Author", "ebook_count": 1, "imagePath": "/image"},
            token="token",
        )
        body = etree.tostring(feed).decode()
        self.assertIn("/opds/proxy/author-image/author-1", body)
        self.assertIn("?token=token", body)
        self.assertIn("1 ebook", body)

    async def test_collection_entry_counts_only_ebooks(self):
        from opds_abs.feeds.collection_feed import CollectionFeedGenerator

        generator = CollectionFeedGenerator()
        feed = generator.create_base_feed()
        generator.add_collection_to_feed(
            "alice", "lib", feed,
            {"id": "collection-1", "name": "Favorites", "books": [
                {"id": "book-1", "media": {"ebookFormat": "epub"}},
                {"id": "audio-1", "media": {}},
            ]},
            token="token",
        )
        body = etree.tostring(feed).decode()
        self.assertIn("Collection with 1 ebook", body)
        self.assertIn("/opds/proxy/cover/book-1", body)

    async def test_series_helpers_filter_ebooks_and_select_common_author(self):
        from opds_abs.feeds.series_feed import SeriesFeedGenerator

        generator = SeriesFeedGenerator()
        items = [
            {"media": {"metadata": {"authors": [{"name": "A"}, {"name": "B"}]}}},
            {"media": {"metadata": {"authors": [{"name": "A"}]}}},
        ]
        self.assertEqual(generator.get_most_common_author(items), "A")
        filtered = generator.filter_series({"results": [
            {
                "id": "series-1",
                "books": [
                    {"id": "book-1", "media": {"ebookFormat": "epub"}},
                    {"id": "audio-1", "media": {}},
                ],
            },
            {
                "id": "series-2",
                "books": [{"id": "audio-2", "media": {}}],
            },
        ]})
        self.assertEqual([series["id"] for series in filtered], ["series-1"])
        self.assertEqual([book["id"] for book in filtered[0]["books"]], ["book-1"])

    async def test_get_most_common_author_skips_blank_author_names(self):
        from opds_abs.feeds.series_feed import SeriesFeedGenerator

        generator = SeriesFeedGenerator()
        items = [
            {"media": {"metadata": {"authors": [{"name": ""}, {"name": "A"}]}}},
            {"media": {"metadata": {"authors": [{"name": "A"}]}}},
        ]
        self.assertEqual(generator.get_most_common_author(items), "A")

    async def test_get_series_display_info_falls_back_to_defaults(self):
        from opds_abs.feeds.series_feed import SeriesFeedGenerator

        generator = SeriesFeedGenerator()
        name, author = generator._get_series_display_info(None, [])
        self.assertEqual(name, "Unknown Series")
        self.assertEqual(author, "Unknown Author")

    async def test_get_series_display_info_uses_series_details_author_without_items(self):
        from opds_abs.feeds.series_feed import SeriesFeedGenerator

        generator = SeriesFeedGenerator()
        name, author = generator._get_series_display_info(
            {"name": "The Series", "authorName": "Jane Doe"}, [])
        self.assertEqual(name, "The Series")
        self.assertEqual(author, "Jane Doe")

    async def test_series_items_fallback_substitutes_library_id_in_url(self):
        """Regression test for a missing f-string prefix in the fallback URL.

        Previously left a literal "{library_id}" in the fallback API URL
        when a series has no book IDs.
        """
        from opds_abs.feeds.series_feed import SeriesFeedGenerator

        generator = SeriesFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [
            {"id": "book-1", "media": {"ebookFormat": "epub"}},
        ]})
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(return_value={
                    "id": "series-1", "name": "Empty Series", "books": []})), \
             patch("opds_abs.feeds.series_feed.fetch_from_api", new=fetch_mock):
            filtered_items, series_details = await generator.filter_items_by_series_id(
                "alice", "lib-42", "series-1", token="tok")

        requested_url = fetch_mock.call_args.args[0]
        self.assertEqual(requested_url, "/libraries/lib-42/items")
        self.assertEqual([item["id"] for item in filtered_items], ["book-1"])
        self.assertEqual(series_details["authorName"], "Unknown Author")

    async def test_search_without_query_returns_valid_empty_feed(self):
        from opds_abs.feeds.search_feed import SearchFeedGenerator

        response = await SearchFeedGenerator().generate_search_feed("alice", "lib")
        body = response.body.decode()
        self.assertEqual(response.media_type, "application/atom+xml")
        self.assertIn("Search results for:", body)

    async def test_search_ebook_detection(self):
        from opds_abs.feeds.search_feed import SearchFeedGenerator

        generator = SearchFeedGenerator()
        self.assertTrue(generator._has_ebook_file({"media": {"ebookFormat": "epub"}}))
        self.assertTrue(generator._has_ebook_file({"media": {"ebookFile": {"ino": "1"}}}))
        self.assertFalse(generator._has_ebook_file({"media": {}}))

    async def test_library_root_redirects_for_single_library(self):
        from opds_abs.feeds.library_feed import LibraryFeedGenerator

        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value={"libraries": [{"id": "lib-1"}]})):
            response = await LibraryFeedGenerator().generate_root_feed("alice", token="token")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/opds/alice/libraries/lib-1")

    async def test_library_root_lists_all_libraries_when_more_than_one(self):
        from opds_abs.feeds.library_feed import LibraryFeedGenerator

        libraries = {"libraries": [
            {"id": "lib-1", "name": "Fiction"}, {"id": "lib-2", "name": "Non-Fiction"}]}
        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value=libraries)):
            response = await LibraryFeedGenerator().generate_root_feed("alice", token="token")
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fiction", body)
        self.assertIn("Non-Fiction", body)
        self.assertIn("/opds/alice/libraries/lib-1", body)
        self.assertIn("/opds/alice/libraries/lib-2", body)

    async def test_library_root_redirect_strips_backslashes_from_username(self):
        # A username containing a backslash could otherwise be used to build a
        # protocol-relative redirect target (some browsers normalize "\" to "/"),
        # e.g. "/opds/\\evil.com/libraries/lib-1" -> "//evil.com/libraries/lib-1".
        # The fix strips backslashes before redirecting, so no such sequence
        # ever reaches the Location header.
        from opds_abs.feeds.library_feed import LibraryFeedGenerator

        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value={"libraries": [{"id": "lib-1"}]})):
            response = await LibraryFeedGenerator().generate_root_feed(
                "\\evil.com", token="token")
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertNotIn("\\", location)
        self.assertFalse(location.startswith("//"))
        self.assertNotIn("://", location)

    async def test_library_root_falls_back_to_full_list_when_target_has_a_netloc(self):
        from opds_abs.feeds import library_feed as library_feed_module

        unsafe_parsed = MagicMock(netloc="evil.example.com", scheme="")
        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value={
                    "libraries": [{"id": "lib-1", "name": "Fiction"}]})), \
             patch.object(library_feed_module, "urlparse", return_value=unsafe_parsed):
            response = await library_feed_module.LibraryFeedGenerator().generate_root_feed(
                "alice", token="token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fiction", response.body.decode())

    async def test_library_items_feed_lists_ebooks_from_api(self):
        from opds_abs.feeds.library_feed import LibraryFeedGenerator

        items_data = {"results": [
            {"id": "book-1", "addedAt": 1,
             "media": {"ebookFormat": "epub", "metadata": {"title": "Dune"}}},
        ]}
        ebook_inos = [{"ino": "999", "filename": "dune.epub", "download_url": "", "token": "tok"}]
        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value=items_data)), \
             patch(
                "opds_abs.feeds.library_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=ebook_inos)):
            response = await LibraryFeedGenerator().generate_library_items_feed(
                "alice", "lib-1", params={}, token="token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Dune", response.body.decode())

    async def test_authors_feed_lists_authors_with_ebooks(self):
        from opds_abs.feeds.author_feed import AuthorFeedGenerator

        generator = AuthorFeedGenerator()
        authors = [{"id": "author-1", "name": "Frank Herbert", "ebook_count": 1}]
        with patch.object(
                generator, "get_authors_with_ebooks", new=AsyncMock(return_value=authors)):
            response = await generator.generate_authors_feed("alice", "lib-1", token="token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Frank Herbert", response.body.decode())

    async def test_authors_feed_reports_when_no_authors_have_ebooks(self):
        from opds_abs.feeds.author_feed import AuthorFeedGenerator

        generator = AuthorFeedGenerator()
        with patch.object(
                generator, "get_authors_with_ebooks", new=AsyncMock(return_value=[])):
            response = await generator.generate_authors_feed("alice", "lib-1", token="token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No authors with ebooks found", response.body.decode())

    async def test_collections_feed_lists_collections_with_ebooks(self):
        from opds_abs.feeds.collection_feed import CollectionFeedGenerator

        async def fake_fetch(url, *args, **kwargs):
            if url.endswith("/collections"):
                return {"results": [{"id": "col-1", "name": "Favorites"}]}
            return {
                "id": "col-1", "name": "Favorites",
                "books": [{"id": "book-1", "media": {"ebookFormat": "epub"}}],
            }

        generator = CollectionFeedGenerator()
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(side_effect=fake_fetch)):
            response = await generator.generate_collections_feed(
                "alice", "lib-1", token="token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Favorites", response.body.decode())

    async def test_search_feed_with_query_returns_valid_feed(self):
        from opds_abs.feeds.search_feed import SearchFeedGenerator

        with patch(
                "opds_abs.feeds.search_feed.get_cached_search_results",
                new=AsyncMock(return_value={})), \
             patch(
                "opds_abs.feeds.search_feed.get_cached_library_items",
                new=AsyncMock(return_value=[])):
            response = await SearchFeedGenerator().generate_search_feed(
                "alice", "lib-1", {"q": "dune"}, token="token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Search results for: dune", response.body.decode())


if __name__ == "__main__":
    unittest.main()
