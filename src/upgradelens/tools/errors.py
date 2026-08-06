"""Exceptions raised by the stage 7 live-document tool layer.

Every failure mode the plan calls out (internal address, too large, redirects,
timeout, rate limit, API degradation) has its own type so callers -- and the
CLI -- can react precisely instead of string-matching an error message.
"""

from __future__ import annotations


class ToolError(Exception):
    """Base class for all tool-layer failures."""


class OutOfNetworkError(ToolError):
    """A host resolved to a private/internal/rejected address, or failed the
    allow-list. This is the SSRF guard firing."""


class TooLargeError(ToolError):
    """The response exceeded the configured byte ceiling."""


class TooManyRedirectsError(ToolError):
    """The response chain exceeded the configured redirect budget."""


class FetchTimeoutError(ToolError):
    """The upstream did not respond within the configured timeout."""


class RateLimitError(ToolError):
    """The upstream (or our own local throttle) rejected the request."""


class ApiDegradedError(ToolError):
    """An upstream API returned a soft failure (e.g. GitHub 403 rate limit) that
    we treat as 'degrade gracefully', not 'crash the run'."""


class HttpError(ToolError):
    """An upstream returned a non-redirect HTTP error status."""

    def __init__(self, status: int, url: str, message: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__(message or f"HTTP {status} for {url}")
