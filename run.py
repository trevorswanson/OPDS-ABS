"""OPDS Feed for Audiobookshelf - Entry Point.

This script serves as the entry point for the OPDS-ABS application.
It configures and starts the uvicorn ASGI server with appropriate settings
based on the configured log level.

The server runs on all network interfaces (0.0.0.0) on port 8000 with
hot reload disabled by default for production/container startup.
"""
import uvicorn
import logging
from opds_abs.config import (
    LOG_LEVEL,
    AUDIOBOOKSHELF_INTERNAL_URL,
    AUDIOBOOKSHELF_EXTERNAL_URL,
    AUDIOBOOKSHELF_API,
    RELOAD_ENABLED
)

# Set up logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger("opds_abs")

def run_server():
    """Start the Uvicorn server with the configured reload policy."""
    # Log URL configurations
    logger.info("-" * 50)
    logger.info("Starting OPDS-ABS server with the following configuration:")
    logger.info(f"AUDIOBOOKSHELF_INTERNAL_URL: {AUDIOBOOKSHELF_INTERNAL_URL}")
    logger.info(f"AUDIOBOOKSHELF_EXTERNAL_URL: {AUDIOBOOKSHELF_EXTERNAL_URL}")
    logger.info(f"AUDIOBOOKSHELF_API: {AUDIOBOOKSHELF_API}")
    logger.info("-" * 50)

    # Use the configured log level, converted to lowercase for Uvicorn
    log_level = LOG_LEVEL.lower()

    # Run uvicorn with the specified log level and enable colored logs
    uvicorn.run(
        "opds_abs.main:app",
        host="0.0.0.0",
        port=8000,
        reload=RELOAD_ENABLED,
        log_level=log_level,
        use_colors=True
    )


if __name__ == "__main__":
    run_server()
