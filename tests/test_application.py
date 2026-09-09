"""Application-level tests for OPDS feeds, utilities, and HTTP boundaries."""

import asyncio
import base64
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

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
        self.assertNotIn("secret", xml)

    def test_pagination_metadata_and_links_are_generated(self):
        feed = self.generator.create_base_feed()
        self.generator.add_pagination_metadata(feed, page=2, items_per_page=10, total_items=25)
        self.generator.add_pagination_links(feed, "alice/libraries/lib/items", 2, 10, 25)
        xml = etree.tostring(feed).decode()
        self.assertIn(">11</opensearch:startIndex>", xml)
        self.assertIn('rel="previous"', xml)
        self.assertIn('start_index=21', xml)


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
        filtered = generator.filter_series({"results": [{
            "id": "series-1",
            "books": [
                {"id": "book-1", "media": {"ebookFormat": "epub"}},
                {"id": "audio-1", "media": {}},
            ],
        }]})
        self.assertEqual([book["id"] for book in filtered[0]["books"]], ["book-1"])

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


if __name__ == "__main__":
    unittest.main()
