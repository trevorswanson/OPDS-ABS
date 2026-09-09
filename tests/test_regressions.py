"""Regression tests for container startup and authenticated feed links."""

import importlib
import os
import sys
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()