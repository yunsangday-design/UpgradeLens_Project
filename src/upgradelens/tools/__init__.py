"""Stage 7 tool layer: the only outbound-IO surface for document acquisition.

Everything here is built so the rest of the system can say, truthfully, "the
LLM never talks to the network." Documents arrive via traced, SSRF-restricted,
cache-first tools; every call is recorded in a :class:`ToolTrace`.
"""

from __future__ import annotations

from upgradelens.tools.cache import CacheEntry, DocCache
from upgradelens.tools.errors import (
    ApiDegradedError,
    FetchTimeoutError,
    HttpError,
    OutOfNetworkError,
    RateLimitError,
    TooLargeError,
    ToolError,
    ToolExecutionError,
    ToolInputError,
    TooManyRedirectsError,
)
from upgradelens.tools.fetcher import FetchConfig, FetchResult, RestrictedFetcher
from upgradelens.tools.github import GitHubClient, Release, shallow_clone, validate_ref
from upgradelens.tools.ingest_live import ingest_live_source, ingest_pypi_changelog
from upgradelens.tools.live_repo import (
    LiveRepoHandle,
    clone_live_repo,
    is_repo_url,
    parse_repo_slug,
)
from upgradelens.tools.pypi import ChangelogEntry, PyPIClient
from upgradelens.tools.trace import ToolCallEvent, ToolTrace
from upgradelens.tools.trust import infer_trust, trust_for_url

__all__ = [
    "CacheEntry",
    "DocCache",
    "ApiDegradedError",
    "FetchTimeoutError",
    "HttpError",
    "OutOfNetworkError",
    "RateLimitError",
    "ToolError",
    "ToolExecutionError",
    "ToolInputError",
    "TooLargeError",
    "TooManyRedirectsError",
    "FetchConfig",
    "FetchResult",
    "RestrictedFetcher",
    "GitHubClient",
    "Release",
    "shallow_clone",
    "validate_ref",
    "ingest_live_source",
    "ingest_pypi_changelog",
    "LiveRepoHandle",
    "clone_live_repo",
    "is_repo_url",
    "parse_repo_slug",
    "ChangelogEntry",
    "PyPIClient",
    "ToolCallEvent",
    "ToolTrace",
    "infer_trust",
    "trust_for_url",
]
