"""Expanded regression coverage for author, collection, series, and search feeds.

See ``tests/test_application.py`` for the original suite and
``tests/test_coverage_expansion.py`` for auth/cache/client coverage.
"""

import unittest
from unittest.mock import AsyncMock, patch

from lxml import etree

from opds_abs.feeds.author_feed import AuthorFeedGenerator
from opds_abs.feeds.collection_feed import CollectionFeedGenerator
from opds_abs.feeds.series_feed import SeriesFeedGenerator
from opds_abs.feeds.search_feed import SearchFeedGenerator
from opds_abs.utils.error_utils import FeedGenerationError, ResourceNotFoundError


def book(book_id, ebook_format="epub", author=None, title=None, series=None):
    """Build a minimal library item dict with an ebook file."""
    author_name = author or "Author"
    metadata = {
        "title": title or book_id,
        "authorName": author_name,
        "authors": [{"name": author_name}],
    }
    if series:
        metadata["series"] = series
    return {
        "id": book_id,
        "addedAt": 0,
        "media": {"ebookFormat": ebook_format, "metadata": metadata},
    }


# ---------------------------------------------------------------------------
# AuthorFeedGenerator
# ---------------------------------------------------------------------------

class AuthorByIdTests(unittest.IsolatedAsyncioTestCase):
    """Verify author lookup by ID."""

    async def test_found_and_not_found(self):
        generator = AuthorFeedGenerator()
        authors = [{"id": "a1", "name": "Herbert"}]
        with patch.object(
                generator, "get_authors_with_ebooks", new=AsyncMock(return_value=authors)):
            found = await generator.get_author_by_id("alice", "lib-1", "a1")
            missing = await generator.get_author_by_id("alice", "lib-1", "missing")
        self.assertEqual(found["name"], "Herbert")
        self.assertEqual(missing, {})


class FilterItemsByAuthorIdTests(unittest.IsolatedAsyncioTestCase):
    """Verify author-based item filtering, including fallbacks."""

    async def test_uses_cached_items_when_author_name_known(self):
        generator = AuthorFeedGenerator()
        items = [
            book("b1", author="Herbert"),
            book("b2", author="Other"),
        ]
        with patch.object(
                generator, "get_author_by_id",
                new=AsyncMock(return_value={"name": "Herbert"})), \
             patch(
                "opds_abs.feeds.author_feed.get_cached_library_items",
                new=AsyncMock(return_value=items)):
            result = await generator.filter_items_by_author_id("alice", "lib-1", "a1")
        self.assertEqual([b["id"] for b in result], ["b1"])

    async def test_falls_back_to_api_when_name_unknown(self):
        generator = AuthorFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [book("b1")]})
        with patch.object(
                generator, "get_author_by_id", new=AsyncMock(return_value={})), \
             patch("opds_abs.feeds.author_feed.fetch_from_api", new=fetch_mock):
            result = await generator.filter_items_by_author_id("alice", "lib-1", "a1")
        self.assertEqual([b["id"] for b in result], ["b1"])
        fetch_mock.assert_awaited_once()

    async def test_falls_back_to_api_on_exception(self):
        generator = AuthorFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [book("b1")]})
        with patch.object(
                generator, "get_author_by_id", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("opds_abs.feeds.author_feed.fetch_from_api", new=fetch_mock):
            result = await generator.filter_items_by_author_id("alice", "lib-1", "a1")
        self.assertEqual([b["id"] for b in result], ["b1"])


class GenerateAuthorItemsFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify per-author item feed generation, pagination, and errors."""

    async def test_no_books_returns_message_entry(self):
        generator = AuthorFeedGenerator()
        with patch.object(
                generator, "_prepare_author_feed",
                new=AsyncMock(return_value=("Herbert", [], generator.create_base_feed()))):
            response = await generator.generate_author_items_feed("alice", "lib-1", "a1")
        self.assertIn("No books found", response.body.decode())

    async def test_pagination_produces_links_and_books(self):
        generator = AuthorFeedGenerator()
        items = [book("b1"), book("b2")]
        with patch.object(
                generator, "_prepare_author_feed",
                new=AsyncMock(
                    return_value=("Herbert", items, generator.create_base_feed()))), \
             patch(
                "opds_abs.feeds.author_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_author_items_feed(
                "alice", "lib-1", "a1", page=1, per_page=1)
        body = response.body.decode()
        self.assertIn('rel="next"', body)

    async def test_unexpected_error_returns_error_response(self):
        generator = AuthorFeedGenerator()
        with patch.object(
                generator, "_prepare_author_feed",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = await generator.generate_author_items_feed("alice", "lib-1", "a1")
        self.assertEqual(response.status_code, 500)


class AddAuthorToFeedTests(unittest.TestCase):
    """Verify author entry construction, including error wrapping."""

    def test_default_cover_used_when_no_image_path(self):
        generator = AuthorFeedGenerator()
        feed = generator.create_base_feed()
        generator.add_author_to_feed(
            "alice", "lib-1", feed, {"id": "a1", "name": "Herbert", "ebook_count": 2})
        body = etree.tostring(feed).decode()
        self.assertIn("unknown-author.png", body)
        self.assertIn("2 ebooks", body)

    def test_raises_feed_generation_error_on_bad_data(self):
        generator = AuthorFeedGenerator()

        class ExplodingDict(dict):
            def get(self, key, default=None):
                if key == "imagePath":
                    raise ValueError("boom")
                return super().get(key, default)

        with self.assertRaises(FeedGenerationError):
            generator.add_author_to_feed(
                "alice", "lib-1", generator.create_base_feed(),
                ExplodingDict(id="a1", name="Herbert"))

    def test_raises_feed_generation_error_on_unexpected_error(self):
        generator = AuthorFeedGenerator()

        class ExplodingDict(dict):
            def get(self, key, default=None):
                if key == "imagePath":
                    raise RuntimeError("boom")
                return super().get(key, default)

        with self.assertRaises(FeedGenerationError):
            generator.add_author_to_feed(
                "alice", "lib-1", generator.create_base_feed(),
                ExplodingDict(id="a1", name="Herbert"))


class PrepareAuthorFeedIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Verify _prepare_author_feed's real (unmocked) integration path."""

    async def test_builds_feed_metadata_from_real_lookups(self):
        generator = AuthorFeedGenerator()
        items = [book("b2", title="Zeta"), book("b1", title="Alpha")]
        with patch.object(
                generator, "get_author_by_id",
                new=AsyncMock(return_value={"id": "a1", "name": "Herbert"})), \
             patch.object(
                generator, "filter_items_by_author_id",
                new=AsyncMock(return_value=list(items))):
            author_name, library_items, feed = await generator._prepare_author_feed(
                "alice", "lib-1", "a1", "tok")
        self.assertEqual(author_name, "Herbert")
        self.assertEqual([b["id"] for b in library_items], ["b1", "b2"])
        self.assertIn("Books by Herbert", etree.tostring(feed).decode())

    async def test_generate_author_items_feed_full_integration_with_pagination(self):
        generator = AuthorFeedGenerator()
        items = [book(f"b{i}", title=f"Title{i}") for i in range(3)]
        with patch.object(
                generator, "get_author_by_id",
                new=AsyncMock(return_value={"id": "a1", "name": "Herbert"})), \
             patch.object(
                generator, "filter_items_by_author_id",
                new=AsyncMock(return_value=list(items))), \
             patch(
                "opds_abs.feeds.author_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[{"ino": "1"}])):
            response = await generator.generate_author_items_feed(
                "alice", "lib-1", "a1", page=2, per_page=1)
        body = response.body.decode()
        self.assertIn('rel="first"', body)
        self.assertIn('rel="previous"', body)


class GetAuthorsWithEbooksTests(unittest.IsolatedAsyncioTestCase):
    """Verify the authors-with-ebooks aggregation and its error paths."""

    async def test_returns_authors_list_on_success(self):
        generator = AuthorFeedGenerator()
        authors = [{"id": "a1", "name": "Herbert", "ebook_count": 2}]
        with patch(
                "opds_abs.feeds.author_feed.get_cached_author_details",
                new=AsyncMock(return_value=authors)):
            result = await generator.get_authors_with_ebooks("alice", "lib-1")
        self.assertEqual(result, authors)

    async def test_raises_resource_not_found_when_empty(self):
        generator = AuthorFeedGenerator()
        with patch(
                "opds_abs.feeds.author_feed.get_cached_author_details",
                new=AsyncMock(return_value=[])):
            with self.assertRaises(ResourceNotFoundError):
                await generator.get_authors_with_ebooks("alice", "lib-1")

    async def test_wraps_unexpected_error(self):
        generator = AuthorFeedGenerator()
        with patch(
                "opds_abs.feeds.author_feed.get_cached_author_details",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(FeedGenerationError):
                await generator.get_authors_with_ebooks("alice", "lib-1")


class GenerateAuthorsFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify the authors listing feed, including pagination and error entries."""

    async def test_pagination_links_appear_across_pages(self):
        generator = AuthorFeedGenerator()
        authors = [
            {"id": "a1", "name": "Adams", "ebook_count": 1},
            {"id": "a2", "name": "Baker", "ebook_count": 1},
        ]
        with patch.object(
                generator, "get_authors_with_ebooks", new=AsyncMock(return_value=authors)):
            response = await generator.generate_authors_feed(
                "alice", "lib-1", page=2, per_page=1)
        body = response.body.decode()
        self.assertIn("Baker", body)
        self.assertIn('rel="previous"', body)

    async def test_resource_not_found_adds_error_entry(self):
        generator = AuthorFeedGenerator()
        with patch.object(
                generator, "get_authors_with_ebooks",
                new=AsyncMock(side_effect=ResourceNotFoundError("none found"))):
            response = await generator.generate_authors_feed("alice", "lib-1")
        self.assertIn(b"Resource not found", response.body)

    async def test_feed_generation_error_adds_error_entry(self):
        generator = AuthorFeedGenerator()
        with patch.object(
                generator, "get_authors_with_ebooks",
                new=AsyncMock(side_effect=FeedGenerationError("broke"))):
            response = await generator.generate_authors_feed("alice", "lib-1")
        self.assertIn(b"Error processing authors", response.body)


# ---------------------------------------------------------------------------
# CollectionFeedGenerator
# ---------------------------------------------------------------------------

class CollectionDetailsAndFilterTests(unittest.IsolatedAsyncioTestCase):
    """Verify collection detail fetches and item filtering fallbacks."""

    async def test_get_collection_details_returns_none_on_error(self):
        generator = CollectionFeedGenerator()
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await generator.get_collection_details("alice", "c1")
        self.assertIsNone(result)

    async def test_filter_by_collection_uses_cached_items(self):
        generator = CollectionFeedGenerator()
        details = {"name": "Favorites", "books": [{"id": "b1"}]}
        items = [book("b1"), book("b2")]
        with patch.object(
                generator, "get_collection_details", new=AsyncMock(return_value=details)), \
             patch(
                "opds_abs.feeds.collection_feed.get_cached_library_items",
                new=AsyncMock(return_value=items)):
            result = await generator.filter_items_by_collection_id("alice", "lib-1", "c1")
        self.assertEqual([b["id"] for b in result], ["b1"])

    async def test_filter_falls_back_when_no_details(self):
        generator = CollectionFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [book("b1")]})
        with patch.object(generator, "get_collection_details", new=AsyncMock(return_value=None)), \
             patch("opds_abs.feeds.collection_feed.fetch_from_api", new=fetch_mock):
            result = await generator.filter_items_by_collection_id("alice", "lib-1", "c1")
        self.assertEqual([b["id"] for b in result], ["b1"])

    async def test_filter_returns_empty_when_no_book_ids(self):
        generator = CollectionFeedGenerator()
        details = {"name": "Empty", "books": []}
        with patch.object(generator, "get_collection_details", new=AsyncMock(return_value=details)):
            result = await generator.filter_items_by_collection_id("alice", "lib-1", "c1")
        self.assertEqual(result, [])

    async def test_filter_falls_back_on_exception(self):
        generator = CollectionFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [book("b1")]})
        with patch.object(
                generator, "get_collection_details",
                new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("opds_abs.feeds.collection_feed.fetch_from_api", new=fetch_mock):
            result = await generator.filter_items_by_collection_id("alice", "lib-1", "c1")
        self.assertEqual([b["id"] for b in result], ["b1"])


class GenerateCollectionItemsFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify per-collection item feed generation, pagination, and errors."""

    async def test_no_books_returns_message_entry(self):
        generator = CollectionFeedGenerator()
        with patch.object(
                generator, "_prepare_collection_feed",
                new=AsyncMock(return_value=("Favorites", [], generator.create_base_feed()))):
            response = await generator.generate_collection_items_feed("alice", "lib-1", "c1")
        self.assertIn("No books found", response.body.decode())

    async def test_pagination_produces_links_and_books(self):
        generator = CollectionFeedGenerator()
        items = [book("b1"), book("b2")]
        with patch.object(
                generator, "_prepare_collection_feed",
                new=AsyncMock(
                    return_value=("Favorites", items, generator.create_base_feed()))), \
             patch(
                "opds_abs.feeds.collection_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_collection_items_feed(
                "alice", "lib-1", "c1", page=1, per_page=1)
        self.assertIn('rel="next"', response.body.decode())

    async def test_unexpected_error_returns_error_response(self):
        generator = CollectionFeedGenerator()
        with patch.object(
                generator, "_prepare_collection_feed",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = await generator.generate_collection_items_feed("alice", "lib-1", "c1")
        self.assertEqual(response.status_code, 500)


class AddCollectionToFeedTests(unittest.TestCase):
    """Verify collection entry construction and default cover fallback."""

    def test_default_cover_used_when_no_ebook_books(self):
        generator = CollectionFeedGenerator()
        feed = generator.create_base_feed()
        generator.add_collection_to_feed(
            "alice", "lib-1", feed, {"id": "c1", "name": "Empty", "books": []})
        body = etree.tostring(feed).decode()
        self.assertIn("collections.png", body)
        self.assertIn("Collection with 0 ebooks", body)

    def test_raises_feed_generation_error_on_key_error(self):
        generator = CollectionFeedGenerator()

        class ExplodingDict(dict):
            def get(self, key, default=None):
                if key == "books":
                    raise KeyError("books")
                return super().get(key, default)

        with self.assertRaises(FeedGenerationError):
            generator.add_collection_to_feed(
                "alice", "lib-1", generator.create_base_feed(),
                ExplodingDict(id="c1", name="Broken"))

    def test_raises_feed_generation_error_on_unexpected_error(self):
        generator = CollectionFeedGenerator()

        class ExplodingDict(dict):
            def get(self, key, default=None):
                if key == "books":
                    raise RuntimeError("boom")
                return super().get(key, default)

        with self.assertRaises(FeedGenerationError):
            generator.add_collection_to_feed(
                "alice", "lib-1", generator.create_base_feed(),
                ExplodingDict(id="c1", name="Broken"))


class PrepareCollectionFeedIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Verify _prepare_collection_feed's real (unmocked) integration path."""

    async def test_builds_feed_metadata_from_real_lookups(self):
        generator = CollectionFeedGenerator()
        with patch.object(
                generator, "get_collection_details",
                new=AsyncMock(return_value={"name": "Favorites"})), \
             patch.object(
                generator, "get_items_in_collection",
                new=AsyncMock(return_value=[book("b1")])):
            name, items, feed = await generator._prepare_collection_feed(
                "alice", "lib-1", "c1", "tok")
        self.assertEqual(name, "Favorites")
        self.assertEqual([b["id"] for b in items], ["b1"])
        self.assertIn("Favorites", etree.tostring(feed).decode())

    async def test_generate_collection_items_feed_full_integration_with_pagination(self):
        generator = CollectionFeedGenerator()
        items = [book(f"b{i}") for i in range(3)]
        with patch.object(
                generator, "get_collection_details",
                new=AsyncMock(return_value={"name": "Favorites"})), \
             patch.object(
                generator, "get_items_in_collection",
                new=AsyncMock(return_value=list(items))), \
             patch(
                "opds_abs.feeds.collection_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[{"ino": "1"}])):
            response = await generator.generate_collection_items_feed(
                "alice", "lib-1", "c1", page=2, per_page=1)
        body = response.body.decode()
        self.assertIn('rel="first"', body)
        self.assertIn('rel="previous"', body)


class GenerateCollectionsFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify the collections listing feed and its aggregate error reporting."""

    async def test_all_collections_failing_adds_aggregate_error_entry(self):
        generator = CollectionFeedGenerator()
        collections = [{"id": "c1", "name": "Broken"}]
        with patch.object(
                generator, "get_collections", new=AsyncMock(return_value=collections)), \
             patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(side_effect=RuntimeError("upstream down"))):
            response = await generator.generate_collections_feed("alice", "lib-1")
        self.assertIn(b"Error retrieving some collections", response.body)

    async def test_resource_not_found_returns_error_feed(self):
        generator = CollectionFeedGenerator()
        with patch.object(
                generator, "get_collections",
                new=AsyncMock(side_effect=ResourceNotFoundError("none"))):
            response = await generator.generate_collections_feed("alice", "lib-1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Collections not found", response.body)

    async def test_get_collections_raises_resource_not_found(self):
        generator = CollectionFeedGenerator()
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(side_effect=RuntimeError("not found please"))):
            with self.assertRaises(ResourceNotFoundError):
                await generator.get_collections("alice", "lib-1")

    async def test_get_collections_reraises_unrelated_error(self):
        generator = CollectionFeedGenerator()
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(side_effect=RuntimeError("upstream unreachable"))):
            with self.assertRaises(RuntimeError):
                await generator.get_collections("alice", "lib-1")

    async def test_get_collections_logs_warning_when_empty(self):
        generator = CollectionFeedGenerator()
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(return_value={"results": []})):
            result = await generator.get_collections("alice", "lib-1")
        self.assertEqual(result, [])

    async def test_get_collections_with_ebooks_skips_entries_without_id(self):
        generator = CollectionFeedGenerator()
        collections = [{"name": "No ID"}]
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock()) as fetch_mock:
            filtered, errors = await generator._get_collections_with_ebooks(
                "alice", collections, None)
        self.assertEqual(filtered, [])
        self.assertEqual(errors, [])
        fetch_mock.assert_not_awaited()

    async def test_get_collections_with_ebooks_records_resource_not_found(self):
        generator = CollectionFeedGenerator()
        collections = [{"id": "c1", "name": "Missing"}]
        with patch(
                "opds_abs.feeds.collection_feed.fetch_from_api",
                new=AsyncMock(side_effect=ResourceNotFoundError("gone"))):
            filtered, errors = await generator._get_collections_with_ebooks(
                "alice", collections, None)
        self.assertEqual(filtered, [])
        self.assertIn("Missing", errors[0])

    async def test_get_items_in_collection_delegates_to_filter(self):
        generator = CollectionFeedGenerator()
        with patch.object(
                generator, "filter_items_by_collection_id",
                new=AsyncMock(return_value=["item"])) as filter_mock:
            result = await generator.get_items_in_collection("alice", "lib-1", "c1")
        self.assertEqual(result, ["item"])
        filter_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# SeriesFeedGenerator
# ---------------------------------------------------------------------------

class SeriesDetailsTests(unittest.IsolatedAsyncioTestCase):
    """Verify series detail lookup and its error handling."""

    async def test_found_and_not_found(self):
        generator = SeriesFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [{"id": "s1", "name": "Dune"}]})
        with patch("opds_abs.feeds.series_feed.fetch_from_api", new=fetch_mock):
            found = await generator.get_series_details("alice", "lib-1", "s1")
            missing = await generator.get_series_details("alice", "lib-1", "s2")
        self.assertEqual(found["name"], "Dune")
        self.assertIsNone(missing)

    async def test_returns_none_on_error(self):
        generator = SeriesFeedGenerator()
        with patch(
                "opds_abs.feeds.series_feed.fetch_from_api",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await generator.get_series_details("alice", "lib-1", "s1")
        self.assertIsNone(result)


class FilterItemsBySeriesIdTests(unittest.IsolatedAsyncioTestCase):
    """Verify series-based item filtering, including the cache-miss fallback."""

    async def test_uses_cached_items_when_details_found(self):
        generator = SeriesFeedGenerator()
        details = {"id": "s1", "name": "Dune", "books": [{"id": "b1"}]}
        items = [book("b1", author="Herbert"), book("b2", author="Other")]
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(return_value=details)), \
             patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=items)):
            filtered, series_details = await generator.filter_items_by_series_id(
                "alice", "lib-1", "s1")
        self.assertEqual([b["id"] for b in filtered], ["b1"])
        self.assertEqual(series_details["authorName"], "Herbert")

    async def test_falls_back_to_api_when_no_series_details(self):
        generator = SeriesFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [book("b1", author="Herbert")]})
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(return_value=None)), \
             patch("opds_abs.feeds.series_feed.fetch_from_api", new=fetch_mock):
            filtered, series_details = await generator.filter_items_by_series_id(
                "alice", "lib-1", "s1")
        self.assertEqual([b["id"] for b in filtered], ["b1"])
        self.assertEqual(series_details["name"], "Unknown Series")
        self.assertEqual(series_details["authorName"], "Herbert")

    async def test_falls_back_to_api_when_cache_has_no_matching_items(self):
        generator = SeriesFeedGenerator()
        details = {"id": "s1", "name": "Dune", "books": [{"id": "not-cached"}]}
        cached_items = [book("other")]
        api_items = {"results": [book("not-cached", author="Herbert")]}
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(return_value=details)), \
             patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=cached_items)), \
             patch(
                "opds_abs.feeds.series_feed.fetch_from_api",
                new=AsyncMock(return_value=api_items)):
            filtered, series_details = await generator.filter_items_by_series_id(
                "alice", "lib-1", "s1")
        self.assertEqual([b["id"] for b in filtered], ["not-cached"])

    async def test_wraps_unexpected_error_with_fallback_details(self):
        generator = SeriesFeedGenerator()
        fetch_mock = AsyncMock(return_value={"results": [book("b1", author="Herbert")]})
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("opds_abs.feeds.series_feed.fetch_from_api", new=fetch_mock):
            filtered, series_details = await generator.filter_items_by_series_id(
                "alice", "lib-1", "s1")
        self.assertEqual([b["id"] for b in filtered], ["b1"])
        self.assertEqual(series_details["id"], "s1")


class ResolveSeriesAuthorNameTests(unittest.IsolatedAsyncioTestCase):
    """Verify author-name resolution fallbacks for series entries."""

    async def test_uses_library_item_author_name(self):
        generator = SeriesFeedGenerator()
        items = [book("b1", author="Herbert")]
        with patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=items)):
            result = await generator._resolve_series_author_name(
                "alice", "lib-1", "b1", {}, None)
        self.assertEqual(result, "Herbert")

    async def test_falls_back_to_metadata_when_book_not_in_library_items(self):
        generator = SeriesFeedGenerator()
        with patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=[])):
            result = await generator._resolve_series_author_name(
                "alice", "lib-1", "b1", {"authorName": "Metadata Author"}, None)
        self.assertEqual(result, "Metadata Author")

    async def test_falls_back_to_unknown_when_nothing_found(self):
        generator = SeriesFeedGenerator()
        result = await generator._resolve_series_author_name(
            "alice", "lib-1", None, {}, None)
        self.assertEqual(result, "Unknown Author")

    async def test_swallows_library_item_lookup_error(self):
        generator = SeriesFeedGenerator()
        with patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await generator._resolve_series_author_name(
                "alice", "lib-1", "b1", {"authorName": "Metadata Author"}, None)
        self.assertEqual(result, "Metadata Author")


class AddSeriesToFeedTests(unittest.IsolatedAsyncioTestCase):
    """Verify series entry construction, including the search-feed content format."""

    async def test_from_search_feed_formats_content_with_author_prefix(self):
        generator = SeriesFeedGenerator()
        feed = generator.create_base_feed()
        series = {
            "id": "s1", "name": "Dune",
            "books": [book("b1", author="Herbert")],
            "authorName": "Herbert",
        }
        with patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=[book("b1", author="Herbert")])):
            await generator.add_series_to_feed("alice", "lib-1", feed, series)
        body = etree.tostring(feed).decode()
        self.assertIn("Series by Herbert", body)

    async def test_from_series_feed_uses_plain_author_content(self):
        generator = SeriesFeedGenerator()
        feed = generator.create_base_feed()
        series = {"id": "s1", "name": "Dune", "books": [book("b1", author="Herbert")]}
        with patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=[book("b1", author="Herbert")])):
            await generator.add_series_to_feed("alice", "lib-1", feed, series, token="tok")
        body = etree.tostring(feed).decode()
        self.assertIn("?token=tok", body)
        self.assertNotIn("Series by", body)


class GenerateSeriesFeedsTests(unittest.IsolatedAsyncioTestCase):
    """Verify the series items feed and the all-series listing feed."""

    async def test_series_items_feed_reports_no_books(self):
        generator = SeriesFeedGenerator()
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(return_value={"name": "Dune", "authorName": "Herbert"})), \
             patch(
                "opds_abs.feeds.series_feed.get_cached_series_items",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_series_items_feed("alice", "lib-1", "s1")
        self.assertIn("No books found", response.body.decode())

    async def test_series_items_feed_lists_sorted_books(self):
        generator = SeriesFeedGenerator()
        items = [
            book("b2", author="Herbert", series={"sequence": 2}),
            book("b1", author="Herbert", series={"sequence": 1}),
        ]
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(return_value={"name": "Dune", "authorName": "Herbert"})), \
             patch(
                "opds_abs.feeds.series_feed.get_cached_series_items",
                new=AsyncMock(return_value=items)), \
             patch(
                "opds_abs.feeds.series_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_series_items_feed("alice", "lib-1", "s1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Dune Series by Herbert", response.body.decode())

    async def test_series_items_feed_handles_exception(self):
        generator = SeriesFeedGenerator()
        with patch(
                "opds_abs.feeds.series_feed.get_cached_series_details",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = await generator.generate_series_items_feed("alice", "lib-1", "s1")
        self.assertEqual(response.status_code, 500)

    async def test_generate_series_feed_lists_all_series(self):
        generator = SeriesFeedGenerator()
        data = {"results": [
            {"id": "s1", "name": "Dune", "books": [book("b1", author="Herbert")]},
        ]}
        with patch(
                "opds_abs.feeds.series_feed.fetch_from_api", new=AsyncMock(return_value=data)), \
             patch(
                "opds_abs.feeds.series_feed.get_cached_library_items",
                new=AsyncMock(return_value=[book("b1", author="Herbert")])):
            response = await generator.generate_series_feed("alice", "lib-1")
        self.assertIn("Dune", response.body.decode())


# ---------------------------------------------------------------------------
# SearchFeedGenerator
# ---------------------------------------------------------------------------

class SearchFeedGenerationTests(unittest.IsolatedAsyncioTestCase):
    """Verify end-to-end search feed generation across books, series, authors."""

    async def test_uses_cached_token_when_not_provided(self):
        generator = SearchFeedGenerator()
        search_results_mock = AsyncMock(return_value={})
        with patch(
                "opds_abs.feeds.search_feed.get_token_for_username",
                return_value="cached-tok") as get_token, \
             patch(
                "opds_abs.feeds.search_feed.get_cached_search_results",
                new=search_results_mock), \
             patch(
                "opds_abs.feeds.search_feed.get_cached_library_items",
                new=AsyncMock(return_value=[])):
            await generator.generate_search_feed("alice", "lib-1", {"q": "dune"})
        get_token.assert_called_once_with("alice")
        self.assertEqual(search_results_mock.call_args.kwargs["token"], "cached-tok")

    async def test_token_in_params_is_used_over_cache(self):
        generator = SearchFeedGenerator()
        search_results_mock = AsyncMock(return_value={})
        with patch(
                "opds_abs.feeds.search_feed.get_token_for_username") as get_token, \
             patch(
                "opds_abs.feeds.search_feed.get_cached_search_results",
                new=search_results_mock), \
             patch(
                "opds_abs.feeds.search_feed.get_cached_library_items",
                new=AsyncMock(return_value=[])):
            await generator.generate_search_feed(
                "alice", "lib-1", {"q": "dune", "token": "param-tok"})
        get_token.assert_not_called()
        self.assertEqual(search_results_mock.call_args.kwargs["token"], "param-tok")

    async def test_full_search_adds_books_series_and_authors(self):
        generator = SearchFeedGenerator()
        search_data = {
            "book": [{"libraryItem": {"id": "b1", "media": {"ebookFormat": "epub"}}}],
            "series": [{
                "series": {"id": "s1", "name": "Dune"},
                "books": [{"id": "b1", "media": {"ebookFormat": "epub"}}],
            }],
            "authors": [{"name": "Herbert"}],
        }
        cached_items = [book("b1", author="Herbert", series=[{"id": "s1"}])]
        with patch(
                "opds_abs.feeds.search_feed.get_cached_search_results",
                new=AsyncMock(return_value=search_data)), \
             patch(
                "opds_abs.feeds.search_feed.get_cached_library_items",
                new=AsyncMock(return_value=cached_items)), \
             patch(
                "opds_abs.feeds.search_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[])):
            response = await generator.generate_search_feed(
                "alice", "lib-1", {"q": "dune"}, token="tok")
        body = response.body.decode()
        self.assertIn("Dune", body)
        self.assertIn("Herbert", body)


class ProcessSingleBookTests(unittest.IsolatedAsyncioTestCase):
    """Verify per-book search-result filtering rules."""

    async def test_skips_result_without_library_item_key(self):
        generator = SearchFeedGenerator()
        feed = generator.create_base_feed()
        await generator._process_single_book(feed, {}, "alice", {})
        self.assertEqual(len(feed.findall("entry")), 0)

    async def test_skips_book_without_ebook(self):
        generator = SearchFeedGenerator()
        feed = generator.create_base_feed()
        result = {"libraryItem": {"id": "b1", "media": {}}}
        await generator._process_single_book(feed, result, "alice", {})
        self.assertEqual(len(feed.findall("entry")), 0)

    async def test_uses_cached_item_metadata_when_available(self):
        generator = SearchFeedGenerator()
        feed = generator.create_base_feed()
        result = {"libraryItem": {"id": "b1", "media": {"ebookFormat": "epub"}}}
        book_id_map = {"b1": book("b1", author="Herbert", title="Dune")}
        with patch(
                "opds_abs.feeds.search_feed.get_download_urls_from_item",
                new=AsyncMock(return_value=[{"ino": "99", "token": "tok"}])):
            await generator._process_single_book(feed, result, "alice", book_id_map)
        body = etree.tostring(feed).decode()
        self.assertIn("Dune", body)


class FindSeriesAuthorTests(unittest.TestCase):
    """Verify the author-resolution precedence for series search results."""

    def test_returns_unknown_when_no_series_id(self):
        generator = SearchFeedGenerator()
        self.assertEqual(
            generator._find_series_author(None, [], [], {}), "Unknown Author")

    def test_uses_series_author_map_first(self):
        generator = SearchFeedGenerator()
        series_author_map = {"s1": {"authors": {"Herbert": 2}, "most_common": "Herbert"}}
        result = generator._find_series_author("s1", [], [], series_author_map)
        self.assertEqual(result, "Herbert")

    def test_falls_back_to_cached_item_author_name(self):
        generator = SearchFeedGenerator()
        cached_items = [book("b1", author="Herbert")]
        result = generator._find_series_author(
            "s1", [{"id": "b1"}], cached_items, {})
        self.assertEqual(result, "Herbert")

    def test_falls_back_to_authors_list_when_no_author_name(self):
        generator = SearchFeedGenerator()
        item = book("b1")
        item["media"]["metadata"] = {"authors": [{"name": "Herbert"}]}
        result = generator._find_series_author("s1", [{"id": "b1"}], [item], {})
        self.assertEqual(result, "Herbert")

    def test_returns_unknown_when_nothing_matches(self):
        generator = SearchFeedGenerator()
        result = generator._find_series_author("s1", [{"id": "missing"}], [], {})
        self.assertEqual(result, "Unknown Author")


class BuildSeriesAuthorMapTests(unittest.TestCase):
    """Verify author-frequency aggregation per series."""

    def test_tracks_most_common_author_per_series(self):
        generator = SearchFeedGenerator()
        items = [
            {"media": {"metadata": {
                "authorName": "Herbert", "series": [{"id": "s1"}]}}},
            {"media": {"metadata": {
                "authorName": "Herbert", "series": [{"id": "s1"}]}}},
            {"media": {"metadata": {
                "authorName": "Other", "series": [{"id": "s1"}]}}},
        ]
        result = generator._build_series_author_map(items, {"s1"})
        self.assertEqual(result["s1"]["most_common"], "Herbert")

    def test_ignores_items_without_author_or_series(self):
        generator = SearchFeedGenerator()
        items = [{"media": {"metadata": {}}}]
        result = generator._build_series_author_map(items, {"s1"})
        self.assertEqual(result, {})


class ProcessAuthorsTests(unittest.IsolatedAsyncioTestCase):
    """Verify author search-result aggregation and feed insertion."""

    async def test_only_authors_with_ebooks_are_added(self):
        generator = SearchFeedGenerator()
        feed = generator.create_base_feed()
        search_data = {"authors": [{"name": "Herbert"}, {"name": "NoBooks"}]}
        cached_items = [book("b1", author="Herbert")]
        await generator._process_authors(feed, search_data, "alice", "lib-1", cached_items)
        body = etree.tostring(feed).decode()
        self.assertIn("Herbert", body)
        self.assertNotIn("NoBooks", body)

    async def test_no_op_when_no_authors_in_search_data(self):
        generator = SearchFeedGenerator()
        feed = generator.create_base_feed()
        await generator._process_authors(feed, {}, "alice", "lib-1", [])
        self.assertEqual(len(feed.findall("entry")), 0)


if __name__ == "__main__":
    unittest.main()
