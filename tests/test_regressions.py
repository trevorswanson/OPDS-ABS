"""Regression tests for container startup and authenticated feed links."""

import importlib
import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch


class ReloadConfigurationTests(unittest.TestCase):
    """Verify Docker startup does not enable Uvicorn's file poller."""

    def test_reload_is_disabled_by_default(self):
        old_value = os.environ.pop("OPDS_RELOAD", None)
        try:
            import opds_abs.config as config
            importlib.reload(config)
            self.assertFalse(config.RELOAD_ENABLED)
        finally:
            if old_value is not None:
                os.environ["OPDS_RELOAD"] = old_value
            importlib.reload(sys.modules["opds_abs.config"])

    def test_reload_can_be_enabled_for_development(self):
        old_value = os.environ.get("OPDS_RELOAD")
        os.environ["OPDS_RELOAD"] = "true"
        try:
            import opds_abs.config as config
            importlib.reload(config)
            self.assertTrue(config.RELOAD_ENABLED)
        finally:
            if old_value is None:
                os.environ.pop("OPDS_RELOAD", None)
            else:
                os.environ["OPDS_RELOAD"] = old_value
            importlib.reload(sys.modules["opds_abs.config"])

    def test_server_passes_reload_setting_to_uvicorn(self):
        import run

        with patch.object(run.uvicorn, "run") as uvicorn_run:
            run.run_server()
        self.assertIs(uvicorn_run.call_args.kwargs["reload"], run.RELOAD_ENABLED)


class MainModuleStartupTests(unittest.TestCase):
    """Verify main.py's module-level logging and cache-persistence setup."""

    def test_invalid_log_level_falls_back_to_info(self):
        old_value = os.environ.get("OPDS_LOG_LEVEL")
        os.environ["OPDS_LOG_LEVEL"] = "NOTALEVEL"
        try:
            import opds_abs.config as config
            importlib.reload(config)
            import opds_abs.main as main
            importlib.reload(main)
            self.assertEqual(main.app_logger.level, logging.INFO)
        finally:
            if old_value is None:
                os.environ.pop("OPDS_LOG_LEVEL", None)
            else:
                os.environ["OPDS_LOG_LEVEL"] = old_value
            importlib.reload(sys.modules["opds_abs.config"])
            importlib.reload(sys.modules["opds_abs.main"])

    def test_atexit_save_hook_skipped_when_persistence_disabled(self):
        old_value = os.environ.get("CACHE_PERSISTENCE_ENABLED")
        os.environ["CACHE_PERSISTENCE_ENABLED"] = "false"
        try:
            import opds_abs.config as config
            importlib.reload(config)
            with patch("atexit.register") as mock_register:
                import opds_abs.main as main
                importlib.reload(main)
            mock_register.assert_not_called()
        finally:
            if old_value is None:
                os.environ.pop("CACHE_PERSISTENCE_ENABLED", None)
            else:
                os.environ["CACHE_PERSISTENCE_ENABLED"] = old_value
            importlib.reload(sys.modules["opds_abs.config"])
            importlib.reload(sys.modules["opds_abs.main"])


class CoverLinkTests(unittest.TestCase):
    """Verify feeds point clients at the authenticated proxy."""

    def test_book_cover_uses_opds_proxy(self):
        from opds_abs.core.feed_generator import BaseFeedGenerator

        feed = BaseFeedGenerator().create_base_feed()
        book = {
            "id": "book-123",
            "addedAt": 0,
            "media": {"metadata": {"title": "Test", "authorName": "Author"}, "ebookFormat": "epub"},
        }
        BaseFeedGenerator().add_book_to_feed(
            feed, book, [{"ino": "42"}], token="redacted-token"
        )
        xml = feed.getroottree().getroot()
        links = [link.get("href") for link in xml.iter() if link.tag.endswith("link")]
        self.assertIn("/opds/proxy/cover/book-123", links)


class AuthorFeedRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Verify author pagination and cross-endpoint name matching."""

    async def test_author_page_parameter_returns_later_authors(self):
        from opds_abs.feeds.author_feed import AuthorFeedGenerator

        generator = AuthorFeedGenerator()
        authors = [
            {"name": f"Author {index:02d}", "ebook_count": 1, "id": str(index)}
            for index in range(51)
        ]
        generator.get_authors_with_ebooks = AsyncMock(return_value=authors)

        page, current, total = await generator._get_paged_authors(
            "user", "library", 2, 50, "token"
        )

        self.assertEqual(current, 2)
        self.assertEqual(total, 2)
        self.assertEqual([author["name"] for author in page], ["Author 50"])

    async def test_zero_page_size_disables_author_pagination(self):
        from opds_abs.feeds.author_feed import AuthorFeedGenerator

        generator = AuthorFeedGenerator()
        authors = [{"name": "Robert Pirsig", "ebook_count": 1, "id": "pirsig"}]
        generator.get_authors_with_ebooks = AsyncMock(return_value=authors)

        page, current, total = await generator._get_paged_authors(
            "user", "library", 99, 0, "token"
        )

        self.assertEqual(current, 1)
        self.assertEqual(total, 1)
        self.assertEqual(page, authors)

    def test_author_name_matching_collapses_case_and_whitespace(self):
        from opds_abs.utils.cache_utils import normalize_author_name

        self.assertEqual(
            normalize_author_name("  Robert   Pirsig "),
            normalize_author_name("robert pirsig"),
        )

    async def test_authors_route_passes_query_page_to_feed_generator(self):
        import opds_abs.main as main

        with patch.object(main, "AUTH_ENABLED", False), \
                patch.object(
                    main.author_feed,
                    "generate_authors_feed",
                    new_callable=AsyncMock,
                    return_value="feed",
                ) as generate:
            result = await main.opds_authors(
                "user", "library", page=2, auth_info=(None, None, None)
            )

        self.assertEqual(result, "feed")
        self.assertEqual(generate.call_args.kwargs["page"], 2)


if __name__ == "__main__":
    unittest.main()
