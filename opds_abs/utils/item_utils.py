"""Shared library-item filtering used by feed generators and the cache layer.

This lives in its own module (rather than on BaseFeedGenerator) so that
cache_utils.py can call it directly instead of having every "get_cached_*"
helper accept a filter_items_func parameter just to thread through the one
implementation that's ever used.
"""
from opds_abs.utils.error_utils import FeedGenerationError, log_error


def filter_ebook_items(data):
    """Find items in a library that have an ebook file, sorted by a field in a specific order.

    Args:
        data (dict): The data containing items to filter.

    Returns:
        list: Filtered list of items that have ebook files.

    Raises:
        FeedGenerationError: If there's an error filtering the items.
    """
    try:
        n = 1
        filtered_results = []
        for result in data.get("results", []):
            media = result.get("media", {})
            if "ebookFormat" in media and media.get("ebookFormat", None):
                result.update({"opds_seq": n})
                n += 1
                filtered_results.append(result)

        return filtered_results
    except Exception as e:
        log_error(e, context="Filtering items for ebooks")
        raise FeedGenerationError(f"Error filtering items: {str(e)}") from e
