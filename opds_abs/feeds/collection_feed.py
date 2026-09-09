"""Collections feed generator."""
# Standard library imports
import logging
import asyncio

# Local application imports
from opds_abs.core.feed_generator import BaseFeedGenerator
from opds_abs.api.client import fetch_from_api, get_download_urls_from_item
from opds_abs.config import ITEMS_PER_PAGE, PAGINATION_ENABLED
from opds_abs.utils import dict_to_xml
from opds_abs.utils.cache_utils import get_cached_library_items
from opds_abs.utils.error_utils import (
    FeedGenerationError,
    ResourceNotFoundError,
    log_error,
    handle_exception
)

# Set up logging
logger = logging.getLogger(__name__)


class CollectionFeedGenerator(BaseFeedGenerator):
    """Generator for collections feed.

    This class creates OPDS feeds that list collections from an Audiobookshelf library and
    the books contained within specific collections. It includes methods for fetching
    collection details, filtering items by collection, and generating collection-based feeds.

    Attributes:
        Inherits all attributes from BaseFeedGenerator.
    """

    async def get_collection_details(self, username, collection_id, token=None):
        """Fetch detailed information about a specific collection.

        Args:
            username (str): The username of the authenticated user.
            collection_id (str): ID of the collection to get details for.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            dict: Collection details or None if not found.
        """
        try:
            # Get the collection data
            collection_data = await fetch_from_api(
                    f"/collections/{collection_id}",
                    username=username,
                    token=token
            )
            return collection_data
        except Exception as e:
            logger.error("Error fetching collection details: %s", e)
            return None

    async def filter_items_by_collection_id(self, username, library_id, collection_id, token=None):
        """Filter items by collection ID using cached items when possible.

        Args:
            username (str): The username of the authenticated user.
            library_id (str): ID of the library containing the items.
            collection_id (str): ID of the collection to filter by.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            list: Library items filtered by the specified collection ID.
        """
        try:
            # Get collection details to get the books in this collection
            collection_details = await self.get_collection_details(
                    username,
                    collection_id,
                    token=token
            )

            # If we can't get collection details, fall back to API call
            if not collection_details:
                logger.warning("Could not find collection details for ID %s", collection_id)
                params = {"collection": collection_id}
                data = await fetch_from_api(
                        f"/libraries/{library_id}/items",
                        params,
                        username=username,
                        token=token
                )
                return self.filter_items(data)

            collection_name = collection_details.get("name", "Unknown Collection")
            logger.debug("Filtering items by collection: %s", collection_name)

            # Get the book IDs in this collection - use a set for O(1) lookup
            collection_book_ids = {
                book.get("id") for book in collection_details.get("books", [])
                if book.get("id")
            }

            if not collection_book_ids:
                logger.warning("No book IDs found in collection %s", collection_id)
                return []

            # Try to get all library items from cache using the shared utility function
            library_items = await get_cached_library_items(
                fetch_from_api,
                self.filter_items,
                username,
                library_id,
                token=token
            )

            # Filter the cached items by matching book IDs - using list comprehension for efficiency
            filtered_items = [
                item for item in library_items
                if item.get("id") in collection_book_ids
            ]

            logger.debug("Found %d items in collection %s from cache",
                         len(filtered_items),
                         collection_name
                         )
            return filtered_items

        except Exception as e:
            logger.error("Error filtering items by collection: %s", e)
            # Fall back to API call if there was an error
            params = {"collection": collection_id}
            data = await fetch_from_api(
                    f"/libraries/{library_id}/items",
                    params,
                    username=username,
                    token=token
            )
            return self.filter_items(data)

    async def generate_collection_items_feed(self, username, library_id, collection_id, token=None,
                                             page=1, per_page=None):
        """Generate a feed of items in a specific collection.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            collection_id (str): The ID of the collection to filter by.
            token (str, optional): Authentication token for Audiobookshelf.
            page (int): The page number to display (1-indexed).
            per_page (int): Number of books per page.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        try:
            logger.debug("Generating collection items feed for collection %s (page %d)",
                         collection_id, page)

            collection_name, collection_items, feed = await self._prepare_collection_feed(
                username, library_id, collection_id, token)

            if not collection_items:
                return self._no_collection_books_response(feed, collection_name)

            paged_items, page, total_pages, no_pagination = (
                self._paginate_collection_items(collection_items, page, per_page))

            # Add pagination links only if pagination is enabled
            if not no_pagination:
                self._add_pagination_links(feed, username, library_id,
                                           collection_id, page, total_pages, token)

            await self._add_paged_books_to_feed(feed, paged_items, username, token)

            return self.create_response(feed)

        except Exception as e:
            # Handle any unexpected errors
            context = f"Generating collection items feed for collection {collection_id}"
            log_error(e, context=context)

            # Use handle_exception to return a standardized error response
            return handle_exception(e, context=context)

    async def _prepare_collection_feed(self, username, library_id, collection_id, token):
        """Fetch the collection's name/items and set up the base feed with metadata.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            collection_id (str): The ID of the collection to filter by.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            tuple: (collection_name, collection_items, feed) - the collection's
                display name, its ebook items, and the feed with base metadata set.
        """
        # Get collection details first to get the name
        collection_info = await self.get_collection_details(
                username,
                collection_id,
                token=token
        )
        collection_name = collection_info.get("name", "Unknown Collection")

        # Get items in the collection
        collection_items = await self.get_items_in_collection(
                username,
                library_id,
                collection_id,
                token=token
        )

        # Create the feed
        feed = self.create_base_feed(username, library_id, token=token)

        # Build the feed metadata
        feed_data = {
            "id": {"_text": f"{library_id}/collections/{collection_id}"},
            "title": {"_text": collection_name}
        }

        # Convert feed metadata to XML
        dict_to_xml(feed, feed_data)

        return collection_name, collection_items, feed

    def _no_collection_books_response(self, feed, collection_name):
        """Build the response for a collection with no ebooks in the library.

        Args:
            feed: The XML feed object to add the error entry to.
            collection_name (str): The collection's display name.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        error_data = {
            "entry": {
                "title": {"_text": "No books found"},
                "content": {
                    "_text": (
                        f"No books in the {collection_name} collection with ebooks "
                        "were found in this library."
                    )
                }
            }
        }
        dict_to_xml(feed, error_data)
        return self.create_response(feed)

    @staticmethod
    def _paginate_collection_items(collection_items, page, per_page):
        """Compute the paginated page of collection items to render.

        Args:
            collection_items (list): All ebook items in the collection.
            page (int): The requested page number (1-indexed).
            per_page (int, optional): Requested items per page; None uses config default.

        Returns:
            tuple: (paged_items, page, total_pages, no_pagination)
        """
        # Check if pagination is enabled
        if not PAGINATION_ENABLED:
            # Pagination is disabled, show all items
            per_page = 0
            no_pagination = True
        else:
            # Use items per page from config if not specified
            per_page = ITEMS_PER_PAGE if per_page is None else per_page
            # If per_page is 0, we'll show all items without pagination
            no_pagination = per_page <= 0

        # Apply pagination
        total_books = len(collection_items)
        total_pages = 1 if no_pagination else (
            total_books + per_page - 1) // per_page  # Ceiling division

        # Adjust page number if out of bounds
        if page < 1:
            page = 1
        elif 0 < total_pages < page:
            page = total_pages

        if no_pagination:
            # No pagination, show all items
            paged_items = collection_items
        else:
            # Calculate start and end indices
            start_idx = (page - 1) * per_page
            end_idx = min(start_idx + per_page, total_books)

            # Get the subset of books for this page
            paged_items = collection_items[start_idx:end_idx]

        return paged_items, page, total_pages, no_pagination

    async def _add_paged_books_to_feed(self, feed, paged_items, username, token):
        """Fetch ebook files in batches and add each paged book to the feed.

        Args:
            feed: The XML feed object to add book entries to.
            paged_items (list): The page of books to add.
            username (str): The username of the authenticated user.
            token (str, optional): Authentication token for Audiobookshelf.
        """
        # Get ebook files in optimal batch sizes to avoid overwhelming the server
        batch_size = 5  # Adjust based on server capacity
        tasks = []

        for book in paged_items:
            book_id = book.get("id", "")
            if book_id:
                tasks.append(get_download_urls_from_item(
                    book_id, username=username, token=token))

        # Process in batches if we have a lot of books
        for i in range(0, len(tasks), batch_size):
            batch_tasks = tasks[i:i+batch_size]
            batch_results = await asyncio.gather(*batch_tasks)

            # Add each book from this batch to the feed
            for j, ebook_info in enumerate(batch_results):
                book_index = i + j
                if book_index < len(paged_items):
                    self.add_book_to_feed(feed, paged_items[book_index], ebook_info, "", token)

    def _add_pagination_links(self, feed, username, library_id, collection_id,
                              current_page, total_pages, token=None):
        """Add pagination links to the feed.

        Args:
            feed: The XML feed object to add links to
            username: The username for URLs
            library_id: The library ID for URLs
            collection_id: The collection ID for URLs
            current_page: Current page number
            total_pages: Total number of pages
            token: Optional token to include in URLs
        """
        # Base URL for pagination
        base_url = f"/opds/{username}/libraries/{library_id}/collections/{collection_id}"
        token_param = f"&token={token}" if token else ""

        # Add pagination links
        links = []

        # First page link
        if current_page > 1:
            links.append({
                "_attrs": {
                    "rel": "first",
                    "href": f"{base_url}?page=1{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog"
                }
            })

        # Previous page link
        if current_page > 1:
            links.append({
                "_attrs": {
                    "rel": "previous",
                    "href": f"{base_url}?page={current_page-1}{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog"
                }
            })

        # Next page link
        if current_page < total_pages:
            links.append({
                "_attrs": {
                    "rel": "next",
                    "href": f"{base_url}?page={current_page+1}{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog"
                }
            })

        # Last page link
        if current_page < total_pages:
            links.append({
                "_attrs": {
                    "rel": "last",
                    "href": f"{base_url}?page={total_pages}{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog"
                }
            })

        # Add links to feed
        for link in links:
            dict_to_xml(feed, {"link": link})

    def add_collection_to_feed(self, username, library_id, feed, collection, token=None):
        """Add a collection to the feed.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library.
            feed (Element): The XML feed element to add the collection to.
            collection (dict): The collection data.
            token (str, optional): Authentication token to include in links.

        Raises:
            FeedGenerationError: If there's an error adding the collection to the feed.
        """
        try:
            collection_id = collection.get("id", "")
            collection_name = collection.get("name", "Unknown collection name")

            # Use list comprehension for more efficient filtering of books with ebooks
            books_with_ebooks = [
                book for book in collection.get("books", [])
                if (book.get("media", {}).get("ebookFile") is not None or
                    (
                        book.get("media", {}).get("ebookFormat") is not None
                        and book.get("media", {}).get("ebookFormat")
                    ))
            ]

            # Get the book count for the entry content
            book_count = len(books_with_ebooks)
            cover_url = self._get_collection_ebook_cover(books_with_ebooks)

            # Add token to the collection link if provided
            collection_link = f"/opds/{username}/libraries/{library_id}/collections/{collection_id}"
            if token:
                collection_link = f"{collection_link}?token={token}"

            entry_data = self._build_collection_entry_data(
                collection_name, collection_id, book_count, collection_link, cover_url)

            # Convert dictionary to XML elements
            dict_to_xml(feed, entry_data)

        except (ValueError, KeyError) as e:
            context = f"Adding collection {collection.get('name', 'unknown')} to feed"
            log_error(e, context=context)
            raise FeedGenerationError(f"Failed to add collection to feed: {str(e)}") from e
        except Exception as e:
            context = f"Adding collection {collection.get('name', 'unknown')} to feed"
            log_error(e, context=context)
            raise FeedGenerationError(
                f"Unexpected error adding collection to feed: {str(e)}") from e

    @staticmethod
    def _get_collection_ebook_cover(books_with_ebooks):
        """Find the cover image URL for the first collection book with an ID.

        Args:
            books_with_ebooks (list): Books in the collection that have an ebook.

        Returns:
            str: The cover proxy URL for the first book found, or the default
                collections icon if none have an ID.
        """
        for book in books_with_ebooks:
            book_id = book.get("id")
            if book_id:
                return f"/opds/proxy/cover/{book_id}"
        return "/static/images/collections.png"

    def _build_collection_entry_data(self, collection_name, collection_id, book_count,
                                     collection_link, cover_url):
        """Build the OPDS entry dict for a collection.

        Args:
            collection_name (str): The collection's display name.
            collection_id (str): The collection's ID.
            book_count (int): Number of ebooks in the collection.
            collection_link (str): The link to the collection's items feed.
            cover_url (str): The cover image URL for the entry.

        Returns:
            dict: The entry data structure ready for dict_to_xml().
        """
        return {
            "entry": {
                "title": {"_text": collection_name},
                "id": {"_text": collection_id},
                "updated": {"_text": self.get_current_timestamp()},
                "content": {
                    "_text": (
                        f"Collection with {book_count} "
                        f"ebook{'s' if book_count != 1 else ''}"
                    )
                },
                "link": [
                    {
                        "_attrs": {
                            "href": collection_link,
                            "rel": "subsection",
                            "type": "application/atom+xml;profile=opds-catalog"
                        }
                    },
                    {
                        "_attrs": {
                            "href": cover_url,
                            "rel": "http://opds-spec.org/image",
                            "type": "image/jpeg"
                        }
                    }
                ]
            }
        }

    async def generate_collections_feed(self, username, library_id, token=None):
        """Generate an OPDS feed listing all collections in a library.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        try:
            logger.debug("Generating collections feed for user %s, library %s",
                         username,
                         library_id
                         )

            # Get all collections from the API
            collections = await self.get_collections(username, library_id, token=token)

            # Create the feed
            feed = self.create_base_feed(username, library_id, token=token)

            # Build the feed metadata
            feed_data = {
                "id": {"_text": f"{library_id}/collections"},
                "title": {"_text": f"Collections in {username}'s library"}
            }
            dict_to_xml(feed, feed_data)

            # Filter collections to only include those with books that have ebook files
            filtered_collections, collection_errors = await self._get_collections_with_ebooks(
                username, collections, token)

            # Add each collection to the feed
            for collection in filtered_collections:
                self.add_collection_to_feed(username, library_id, feed, collection, token)

            # If we have no collections but had errors, add an entry about the errors
            if not filtered_collections and collection_errors:
                error_message = "Some collections could not be retrieved:\n" + \
                    "\n".join(collection_errors)
                error_data = {
                    "entry": {
                        "title": {"_text": "Error retrieving some collections"},
                        "content": {"_text": error_message}
                    }
                }
                dict_to_xml(feed, error_data)

            return self.create_response(feed)

        except ResourceNotFoundError as e:
            context = f"Generating collections feed for library {library_id}"
            log_error(e, context=context, log_traceback=False)

            # Return a feed with the specific error
            feed = self.create_base_feed(username, library_id, token=token)
            error_data = {
                "title": {"_text": "Collections not found"},
                "entry": {
                    "content": {"_text": str(e)}
                }
            }
            dict_to_xml(feed, error_data)
            return self.create_response(feed)
        except Exception as e:
            # Handle any other unexpected errors
            context = f"Generating collections feed for user {username}, library {library_id}"
            log_error(e, context=context)

            # Use handle_exception to return a standardized error response
            return handle_exception(e, context=context)

    @staticmethod
    def _count_collection_ebooks(collection_data):
        """Count how many books in a collection have an ebook file or format.

        Args:
            collection_data (dict): A collection's data from the Audiobookshelf API.

        Returns:
            int: The number of books with an ebook.
        """
        ebook_count = 0
        for book in collection_data.get("books", []):
            media = book.get("media", {})
            if (
                    media.get("ebookFile") is not None
                    or (
                        media.get("ebookFormat") is not None
                        and media.get("ebookFormat")
                    )
            ):
                ebook_count += 1
        return ebook_count

    async def _get_collections_with_ebooks(self, username, collections, token):
        """Fetch each collection's books and keep only those containing an ebook.

        Args:
            username (str): The username requesting the feed.
            collections (list): The raw collection list from get_collections().
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            tuple: (filtered_collections, collection_errors) - full collection
                data (with books) for collections that have at least one ebook,
                sorted by name, and a list of human-readable error strings for
                collections that could not be fetched.
        """
        filtered_collections = []
        collection_errors = []

        for collection in collections:
            # We need to fetch each collection's books separately
            collection_id = collection.get("id", "")
            if not collection_id:
                continue

            try:
                collection_data = await fetch_from_api(
                    f"/collections/{collection_id}",
                    username=username,
                    token=token,
                )

                ebook_count = self._count_collection_ebooks(collection_data)
                if ebook_count > 0:
                    logger.debug("Collection \"%s\" has %d ebooks",
                                 collection_data.get('name'), ebook_count)
                    filtered_collections.append(collection_data)
            except ResourceNotFoundError as e:
                context = f"Fetching collection {collection_id}"
                log_error(e, context=context, log_traceback=False)
                collection_errors.append(
                    f"Collection {collection.get('name', collection_id)}: {str(e)}")
            except Exception as e:
                context = f"Fetching collection {collection_id}"
                log_error(e, context=context)
                collection_errors.append(
                    f"Collection {collection.get('name', collection_id)}: {str(e)}")

        # Sort collections by name
        filtered_collections = sorted(
            filtered_collections, key=lambda x: x.get("name", "").lower())

        return filtered_collections, collection_errors

    async def get_collections(self, username, library_id, token=None):
        """Fetch all collections available in a library.

        Args:
            username (str): The username of the authenticated user.
            library_id (str): ID of the library to get collections from.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            list: All collections in the library.

        Raises:
            ResourceNotFoundError: If the library or collections cannot be found.
        """
        try:
            # Make an API call to get all collections
            collections_data = await fetch_from_api(
                f"/libraries/{library_id}/collections",
                {"limit": 1000, "sort": "name"},
                username=username,
                token=token
            )

            # Check if we got any collections
            collections = collections_data.get("results", [])

            if not collections:
                logger.warning("No collections found in library %s", library_id)
            else:
                logger.debug("Found %d collections in library %s", len(collections), library_id)

            return collections

        except Exception as e:
            logger.error("Error fetching collections: %s", e)
            if "not found" in str(e).lower():
                raise ResourceNotFoundError(
                    f"Collections not found for library {library_id}") from e
            raise

    async def get_items_in_collection(self, username, library_id, collection_id, token=None):
        """Get items in a specific collection with ebooks.

        Args:
            username (str): The username of the authenticated user.
            library_id (str): ID of the library containing the collection.
            collection_id (str): ID of the collection to get items from.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            list: Items in the collection that have ebooks.
        """
        # Use the existing filter_items_by_collection_id method to get the items
        return await self.filter_items_by_collection_id(
            username,
            library_id,
            collection_id,
            token=token
        )
