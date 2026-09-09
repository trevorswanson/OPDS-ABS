"""Backward-compatible shim for the centralized application configuration.

New application code must import settings from :mod:`opds_abs.config`.
This module remains so older integrations importing ``config`` continue to
resolve the current settings instead of maintaining a second configuration
implementation.
"""

# pylint: disable=wildcard-import,unused-wildcard-import
from opds_abs.config import *  # noqa: F401,F403
