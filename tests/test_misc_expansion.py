"""Expanded coverage for feed_generator, library_feed, navigation_feed, xml_utils, and more.

See ``tests/test_application.py`` for the original suite and
``tests/test_coverage_expansion.py`` / ``tests/test_feeds_expansion.py`` for
auth/cache/client/feed coverage added alongside this file.
"""

import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from lxml import etree

from opds_abs.core.feed_generator import BaseFeedGenerator
from opds_abs.feeds.library_feed import LibraryFeedGenerator
from opds_abs.feeds.navigation_feed import NavigationFeedGenerator
from opds_abs.utils import xml_utils
from opds_abs.utils.error_utils import (
    APIClientError,
    OPDSBaseException,
    convert_to_http_exception,
    handle_exception,
    log_error,
)


# ---------------------------------------------------------------------------
# core.feed_generator
# ---------------------------------------------------------------------------

class CreateResponseErrorTests(unittest.TestCase):
    """Verify create_response wraps XML serialization failures."""

    def test_serialization_failure_raises_feed_generation_error(self):
        from opds_abs.utils.error_utils import FeedGenerationError

        generator = BaseFeedGenerator()
        with patch.object(
                etree, "tostring", side_effect=ValueError("bad xml")):
            with self.assertRaises(FeedGenerationError):
                generator.create_response(generator.create_base_feed())


class AddBookToFeedErrorTests(unittest.TestCase):
    """Verify both error branches of add_book_to_feed()."""

    def test_key_error_is_wrapped_as_feed_generation_error(self):
        from opds_abs.utils.error_utils import FeedGenerationError

        class ExplodingMetadata(dict):
            def get(self, key, default=None):
                if key == "description":
                    raise KeyError("description")
                return super().get(key, default)

        generator = BaseFeedGenerator()
        feed = generator.create_base_feed()
        book = {
            "id": "book-1", "addedAt": 0,
            "media": {"ebookFormat": "epub", "metadata": ExplodingMetadata(
                title="T", authorName="A")},
        }
        with self.assertRaises(FeedGenerationError):
            generator.add_book_to_feed(feed, book, [{"ino": "1"}])

    def test_unexpected_error_is_wrapped_as_feed_generation_error(self):
        from opds_abs.utils.error_utils import FeedGenerationError

        generator = BaseFeedGenerator()
        feed = generator.create_base_feed()
        # A missing addedAt causes `None / 1000`, a TypeError caught by the
        # generic Exception branch (not the ValueError/KeyError one).
        book = {
            "id": "book-1", "addedAt": None,
            "media": {"ebookFormat": "epub", "metadata": {
                "title": "T", "authorName": "A"}},
        }
        with self.assertRaises(FeedGenerationError):
            generator.add_book_to_feed(feed, book, [{"ino": "1"}])

    def test_token_falls_back_to_ebook_file_token(self):
        generator = BaseFeedGenerator()
        feed = generator.create_base_feed()
        book = {
            "id": "book-1", "addedAt": 0,
            "media": {"ebookFormat": "epub", "metadata": {
                "title": "T", "authorName": "A"}},
        }
        generator.add_book_to_feed(feed, book, [{"ino": "1", "token": "from-ebook"}])
        # No assertion on token in URL (proxy hides it); just confirm no error
        # and an entry was added.
        self.assertEqual(len(feed.findall("entry")), 1)


class FilterAndSortErrorTests(unittest.TestCase):
    """Verify filter_items(), sort_results(), and create_filter() error wrapping."""

    def test_filter_items_wraps_unexpected_error(self):
        from opds_abs.utils.error_utils import FeedGenerationError

        class BadData:
            def get(self, key, default=None):
                raise RuntimeError("boom")

        generator = BaseFeedGenerator()
        with self.assertRaises(FeedGenerationError):
            generator.filter_items(BadData())

    def test_sort_results_wraps_unexpected_error(self):
        from opds_abs.utils.error_utils import FeedGenerationError

        generator = BaseFeedGenerator()
        with self.assertRaises(FeedGenerationError):
            generator.sort_results([{"no_seq_key": 1}])

    def test_create_filter_returns_empty_string_on_error(self):
        generator = BaseFeedGenerator()

        class NotAString:
            def encode(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        result = generator.create_filter(NotAString())
        self.assertEqual(result, "")

    def test_create_filter_none_returns_empty_string(self):
        generator = BaseFeedGenerator()
        self.assertEqual(generator.create_filter(None), "")

    def test_extract_value_handles_error_gracefully(self):
        generator = BaseFeedGenerator()

        class BadDict:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        self.assertIsNone(generator.extract_value(BadDict(), "a.b"))


class PaginateResultsTests(unittest.TestCase):
    """Verify paginate_results edge cases."""

    def test_empty_items_returns_empty_list(self):
        generator = BaseFeedGenerator()
        self.assertEqual(generator.paginate_results([], 1, 10), [])

    def test_zero_items_per_page_returns_all_items(self):
        generator = BaseFeedGenerator()
        items = [1, 2, 3]
        self.assertEqual(generator.paginate_results(items, 1, 0), items)

    def test_out_of_range_start_index_resets_to_zero(self):
        generator = BaseFeedGenerator()
        items = [1, 2, 3]
        self.assertEqual(generator.paginate_results(items, 100, 2), [1, 2])


# ---------------------------------------------------------------------------
# feeds.library_feed
# ---------------------------------------------------------------------------

class GenerateRootFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify the library root feed's redirect and listing branches."""

    async def test_single_library_redirects(self):
        generator = LibraryFeedGenerator()
        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value={"libraries": [{"id": "lib-1", "name": "Main"}]})):
            response = await generator.generate_root_feed("alice", token="tok")
        self.assertEqual(response.status_code, 302)


class PaginationNumberTests(unittest.TestCase):
    """Verify pagination number computation and path building."""

    def test_invalid_start_index_defaults_to_one(self):
        result = LibraryFeedGenerator._compute_pagination_numbers({"start_index": "bogus"})
        self.assertEqual(result[0], 1)

    def test_pagination_disabled_returns_all_items(self):
        with patch("opds_abs.feeds.library_feed.PAGINATION_ENABLED", False):
            start_index, items_per_page, no_pagination, page = (
                LibraryFeedGenerator._compute_pagination_numbers({}))
        self.assertTrue(no_pagination)
        self.assertEqual(items_per_page, 0)
        self.assertEqual(page, 1)

    def test_build_current_paths_includes_filters_and_page(self):
        current_path, current_path_with_page, api_params = (
            LibraryFeedGenerator._build_current_paths(
                "alice", "lib-1", {"sort": "addedAt", "start_index": "5"}, 5, False))
        self.assertIn("sort=addedAt", current_path)
        self.assertIn("start_index=5", current_path_with_page)
        self.assertNotIn("start_index", api_params)

    def test_build_current_paths_no_pagination_omits_start_index(self):
        current_path, current_path_with_page, _ = LibraryFeedGenerator._build_current_paths(
            "alice", "lib-1", {}, 1, True)
        self.assertEqual(current_path, current_path_with_page)


class NormalizeBookEbookFormatTests(unittest.TestCase):
    """Verify ebook format detection/normalization for collection books."""

    def test_derives_format_from_ebook_file_extension(self):
        book = {"media": {"ebookFile": {"metadata": {"ext": ".pdf"}}}}
        self.assertTrue(LibraryFeedGenerator._normalize_book_ebook_format(book))
        self.assertEqual(book["media"]["ebookFormat"], "pdf")

    def test_defaults_to_epub_when_extension_missing(self):
        book = {"media": {"ebookFile": {"metadata": {}}}}
        self.assertTrue(LibraryFeedGenerator._normalize_book_ebook_format(book))
        self.assertEqual(book["media"]["ebookFormat"], "epub")

    def test_existing_ebook_format_is_detected(self):
        book = {"media": {"ebookFormat": "mobi"}}
        self.assertTrue(LibraryFeedGenerator._normalize_book_ebook_format(book))

    def test_no_ebook_returns_false(self):
        book = {"media": {}}
        self.assertFalse(LibraryFeedGenerator._normalize_book_ebook_format(book))


class GenerateLibraryItemsFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify the main library items feed flow, including collection filtering."""

    async def test_collection_filter_success_returns_collection_feed(self):
        generator = LibraryFeedGenerator()
        collection_data = {
            "name": "Favorites",
            "books": [{"id": "b1", "addedAt": 0, "media": {
                "ebookFormat": "epub", "metadata": {"title": "T", "authorName": "A"}}}],
        }
        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(return_value=collection_data)), \
             patch(
                "opds_abs.feeds.library_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_library_items_feed(
                "alice", "lib-1", {"collection": "c1"}, token="tok")
        self.assertIn("Favorites", response.body.decode())

    async def test_collection_filter_failure_falls_back_to_normal_feed(self):
        generator = LibraryFeedGenerator()
        items_data = {"results": [
            {"id": "b1", "addedAt": 0, "media": {
                "ebookFormat": "epub", "metadata": {"title": "T"}}}]}
        with patch(
                "opds_abs.feeds.library_feed.fetch_from_api",
                new=AsyncMock(side_effect=[RuntimeError("boom"), items_data])), \
             patch(
                "opds_abs.feeds.library_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_library_items_feed(
                "alice", "lib-1", {"collection": "c1"}, token="tok")
        self.assertIn("alice's books", response.body.decode())

    async def test_special_sort_feed_uses_cached_items_sorted_by_added_at(self):
        generator = LibraryFeedGenerator()
        cached_items = [
            {"id": "old", "addedAt": 1, "media": {"metadata": {"title": "Old"}}},
            {"id": "new", "addedAt": 100, "media": {"metadata": {"title": "New"}}},
        ]
        with patch(
                "opds_abs.feeds.library_feed.get_cached_library_items",
                new=AsyncMock(return_value=cached_items)), \
             patch(
                "opds_abs.feeds.library_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[{"ino": "1"}])):
            response = await generator.generate_library_items_feed(
                "alice", "lib-1", {"sort": "addedAt"}, token="tok")
        body = response.body.decode()
        self.assertLess(body.index("New"), body.index("Old"))

    async def test_special_sort_feed_uses_cached_items_sorted_by_title(self):
        generator = LibraryFeedGenerator()
        cached_items = [
            {"id": "z", "addedAt": 1, "media": {"metadata": {"title": "Zorro"}}},
            {"id": "a", "addedAt": 2, "media": {"metadata": {"title": "Alpha"}}},
        ]
        with patch(
                "opds_abs.feeds.library_feed.get_cached_library_items",
                new=AsyncMock(return_value=cached_items)), \
             patch(
                "opds_abs.feeds.library_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[{"ino": "1"}])):
            response = await generator.generate_library_items_feed(
                "alice", "lib-1", {"sort": "media.metadata.title"}, token="tok")
        body = response.body.decode()
        self.assertLess(body.index("Alpha"), body.index("Zorro"))


# ---------------------------------------------------------------------------
# feeds.navigation_feed
# ---------------------------------------------------------------------------

class NavigationFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify navigation link construction without a token, and error handling."""

    async def test_without_token_uses_params_only_branch(self):
        response = await NavigationFeedGenerator().generate_navigation_feed("alice", "lib-1")
        body = response.body.decode()
        self.assertIn("sort=media.metadata.title", body)
        self.assertNotIn("token=", body)

    async def test_exception_is_logged_and_reraised(self):
        with patch(
                "opds_abs.feeds.navigation_feed.dict_to_xml",
                side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await NavigationFeedGenerator().generate_navigation_feed("alice", "lib-1")


# ---------------------------------------------------------------------------
# utils.xml_utils
# ---------------------------------------------------------------------------

class DictToXmlScalarTests(unittest.TestCase):
    """Verify the plain scalar (non-dict, non-list) branch of dict_to_xml."""

    def test_scalar_value_sets_text_directly(self):
        root = etree.Element("root")
        xml_utils.dict_to_xml(root, {"count": 5})
        self.assertEqual(root.find("count").text, "5")

    def test_none_value_leaves_element_empty(self):
        root = etree.Element("root")
        xml_utils.dict_to_xml(root, {"empty": None})
        self.assertIsNone(root.find("empty").text)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class ConfigUrlFallbackTests(unittest.TestCase):
    """Verify AUDIOBOOKSHELF_URL legacy fallback and internal/external precedence."""

    def _reload_config_with_env(self, env_overrides):
        """Reload config with the given env vars and snapshot the URL attributes.

        Snapshotting is necessary because restoring the original environment
        also requires reloading the module (to undo the test's env change),
        which would otherwise overwrite a live module reference before the
        caller can inspect it.
        """
        import opds_abs.config as config_module

        env_keys = (
            "AUDIOBOOKSHELF_URL", "AUDIOBOOKSHELF_INTERNAL_URL", "AUDIOBOOKSHELF_EXTERNAL_URL")
        original = {key: os.environ.get(key) for key in env_keys}
        try:
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ.update(env_overrides)
            importlib.reload(config_module)
            return {
                "internal": config_module.AUDIOBOOKSHELF_INTERNAL_URL,
                "external": config_module.AUDIOBOOKSHELF_EXTERNAL_URL,
            }
        finally:
            for key in env_keys:
                os.environ.pop(key, None)
            for key, value in original.items():
                if value is not None:
                    os.environ[key] = value
            importlib.reload(config_module)

    def test_legacy_url_sets_both_internal_and_external(self):
        urls = self._reload_config_with_env({"AUDIOBOOKSHELF_URL": "http://legacy:1234"})
        self.assertEqual(urls["internal"], "http://legacy:1234")
        self.assertEqual(urls["external"], "http://legacy:1234")

    def test_internal_only_is_used_for_both(self):
        urls = self._reload_config_with_env(
            {"AUDIOBOOKSHELF_INTERNAL_URL": "http://internal:1234"})
        self.assertEqual(urls["internal"], "http://internal:1234")
        self.assertEqual(urls["external"], "http://internal:1234")

    def test_external_only_is_used_for_both(self):
        urls = self._reload_config_with_env(
            {"AUDIOBOOKSHELF_EXTERNAL_URL": "http://external:1234"})
        self.assertEqual(urls["internal"], "http://external:1234")
        self.assertEqual(urls["external"], "http://external:1234")


# ---------------------------------------------------------------------------
# utils.error_utils
# ---------------------------------------------------------------------------

class HandleExceptionEdgeCaseTests(unittest.TestCase):
    """Verify handle_exception's HTTPException path and status override."""

    def test_http_exception_client_error_uses_generic_message(self):
        response = handle_exception(
            HTTPException(status_code=404, detail="secret internal path"),
            log_traceback=False)
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Request failed", response.body)
        self.assertNotIn(b"secret internal path", response.body)

    def test_http_exception_server_error_uses_generic_message(self):
        response = handle_exception(
            HTTPException(status_code=503, detail="upstream detail"),
            log_traceback=False)
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"An internal server error occurred", response.body)

    def test_status_code_override_takes_precedence(self):
        response = handle_exception(
            APIClientError("upstream"), status_code=418, log_traceback=False)
        self.assertEqual(response.status_code, 418)

    def test_generic_exception_uses_safe_message(self):
        response = handle_exception(ValueError("leaky detail"), log_traceback=False)
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"An unexpected error occurred", response.body)
        self.assertNotIn(b"leaky detail", response.body)


class ConvertToHttpExceptionTests(unittest.TestCase):
    """Verify convert_to_http_exception's message/status precedence rules."""

    def test_opds_exception_uses_class_status_and_message(self):
        result = convert_to_http_exception(APIClientError("upstream down"))
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.detail, "upstream down")

    def test_http_exception_passthrough(self):
        result = convert_to_http_exception(HTTPException(status_code=403, detail="forbidden"))
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.detail, "forbidden")

    def test_generic_exception_defaults_to_500(self):
        result = convert_to_http_exception(ValueError("oops"))
        self.assertEqual(result.status_code, 500)
        self.assertEqual(result.detail, "oops")

    def test_explicit_overrides_take_precedence(self):
        result = convert_to_http_exception(
            APIClientError("upstream down"), status_code=400, detail="custom")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.detail, "custom")


class LogErrorTests(unittest.TestCase):
    """Verify log_error's traceback toggle and context prefix."""

    def test_logs_with_and_without_traceback(self):
        # Just verify no exception is raised either way.
        log_error(ValueError("boom"), context="somewhere", log_traceback=True)
        log_error(ValueError("boom"), context="somewhere", log_traceback=False)
        log_error(ValueError("boom"))


class OPDSBaseExceptionDefaultsTests(unittest.TestCase):
    """Verify the base exception's default status code and message."""

    def test_defaults(self):
        exc = OPDSBaseException("custom")
        self.assertEqual(exc.status_code, 500)
        self.assertEqual(exc.default_message, "An internal server error occurred")


if __name__ == "__main__":
    unittest.main()
