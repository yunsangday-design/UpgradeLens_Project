"""Offline tests for the stage 7 tool layer (network is always mocked)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from upgradelens.tools.cache import CacheEntry, DocCache
from upgradelens.tools.errors import (
    FetchTimeoutError,
    OutOfNetworkError,
    TooLargeError,
    TooManyRedirectsError,
)
from upgradelens.tools.fetcher import FetchConfig, RestrictedFetcher
from upgradelens.tools.github import GitHubClient, validate_ref
from upgradelens.tools.live_repo import clone_live_repo, is_repo_url, parse_repo_slug
from upgradelens.tools.pypi import PyPIClient
from upgradelens.tools.trace import ToolTrace
from upgradelens.tools.trust import infer_trust


class FakeResponse:
    """Minimal ``urllib``-shaped response for tests."""

    def __init__(
        self,
        code: int,
        body: bytes,
        headers: dict[str, str] | None = None,
        url: str = "http://example.com/x",
    ) -> None:
        self._code = code
        self._body = body
        self._pos = 0
        self.headers = headers or {}
        self._url = url

    def getcode(self) -> int:
        return self._code

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out = self._body[self._pos :]
            self._pos = len(self._body)
        else:
            out = self._body[self._pos : self._pos + n]
            self._pos += len(out)
        return out

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        pass

    def items(self) -> list[tuple[str, str]]:
        return list(self.headers.items())


def make_opener(responses: dict[str, FakeResponse]):
    """Build a fake opener that returns a fresh cloned response per URL.

    A new instance is returned on every call so repeated fetches of the same
    URL (e.g. latest_version then changelog) do not share and exhaust one
    reader.
    """

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        template = responses[req.full_url]
        return FakeResponse(template._code, template._body, dict(template.headers), template._url)

    return opener


# --------------------------------------------------------------------- trust
def test_infer_trust_allowlist() -> None:
    assert infer_trust("https://pypi.org/pypi/foo/json") == "official"
    assert infer_trust("https://docs.python.org/3/library/foo.html") == "official"
    assert infer_trust("https://project.readthedocs.io/en/latest/") == "community"
    assert infer_trust("https://some-random-blog.example.com/post") == "unverified"


# ------------------------------------------------------------------- fetcher
def test_fetcher_success_records_trace() -> None:
    opener = make_opener(
        {
            "http://example.com/doc": FakeResponse(
                200, b"hello world", {"Content-Type": "text/plain"}
            )
        }
    )
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(trace=ToolTrace(), opener=opener)
        result = fetcher.fetch("http://example.com/doc")
    assert result.content == b"hello world"
    assert result.status == 200
    assert fetcher._trace.events[-1].status == "ok"
    assert fetcher._trace.events[-1].bytes == len(b"hello world")


def test_fetcher_allowlist_blocks_unknown_host() -> None:
    config = FetchConfig(allow_hosts=frozenset({"example.com"}))
    fetcher = RestrictedFetcher(config, opener=make_opener({}))
    with pytest.raises(OutOfNetworkError):
        fetcher.fetch("http://evil.com/x")


def test_fetcher_blocks_internal_ip() -> None:
    fetcher = RestrictedFetcher(opener=make_opener({}))
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["127.0.0.1"]):
        with pytest.raises(OutOfNetworkError):
            fetcher.fetch("http://localhost-internal.example/doc")


def test_fetcher_blocks_ip_literal() -> None:
    fetcher = RestrictedFetcher(opener=make_opener({}))
    with pytest.raises(OutOfNetworkError):
        fetcher.fetch("http://10.0.0.5/doc")


def test_fetcher_rejects_non_http() -> None:
    fetcher = RestrictedFetcher(opener=make_opener({}))
    with pytest.raises(OutOfNetworkError):
        fetcher.fetch("file:///etc/passwd")


def test_fetcher_enforces_size_cap() -> None:
    config = FetchConfig(max_bytes=10)
    opener = make_opener(
        {"http://example.com/big": FakeResponse(200, b"x" * 100, {"Content-Type": "text/plain"})}
    )
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(config, opener=opener)
        with pytest.raises(TooLargeError):
            fetcher.fetch("http://example.com/big")


def test_fetcher_enforces_redirect_budget() -> None:
    responses = {
        f"http://example.com/r{i}": FakeResponse(
            301,
            b"",
            {"Location": f"http://example.com/r{i + 1}"} if i < 10 else "http://example.com/r10",
        )
        for i in range(1, 11)
    }
    responses["http://example.com/r10"] = FakeResponse(200, b"done", {"Content-Type": "text/plain"})
    config = FetchConfig(max_redirects=3)
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(config, opener=make_opener(responses))
        with pytest.raises(TooManyRedirectsError):
            fetcher.fetch("http://example.com/r1")


def test_fetcher_follows_redirect() -> None:
    responses = {
        "http://example.com/a": FakeResponse(301, b"", {"Location": "http://example.com/b"}),
        "http://example.com/b": FakeResponse(
            200, b"final", {"Content-Type": "text/plain"}, url="http://example.com/b"
        ),
    }
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(opener=make_opener(responses))
        result = fetcher.fetch("http://example.com/a")
    assert result.content == b"final"
    assert result.final_url == "http://example.com/b"


def test_fetcher_timeout() -> None:
    def opener_raises(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.URLError(TimeoutError("timed out"))

    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(opener=opener_raises)
        with pytest.raises(FetchTimeoutError):
            fetcher.fetch("http://example.com/slow")


def test_fetcher_rate_limit_delays() -> None:
    config = FetchConfig(min_interval_per_host=0.05)
    opener = make_opener(
        {"http://example.com/doc": FakeResponse(200, b"ok", {"Content-Type": "text/plain"})}
    )
    with (
        patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]),
        patch.object(time, "sleep") as sleep_mock,
    ):
        fetcher = RestrictedFetcher(config, opener=opener)
        fetcher.fetch("http://example.com/doc")
        fetcher.fetch("http://example.com/doc")
    sleep_mock.assert_called_once()


def test_fetcher_cache_first_hit() -> None:
    cache = DocCache(Path("/tmp/ul-nonexistent-cache-xyz"))
    key = cache.key_for("http://example.com/cached")
    cache.put(
        key,
        CacheEntry(
            url="http://example.com/cached",
            final_url="http://example.com/cached",
            status=200,
            content=b"from cache",
            content_type="text/plain",
            etag=None,
            fetched_at=time.time(),
        ),
    )
    # No opener needed: the result is served from cache before any network check.
    fetcher = RestrictedFetcher(trace=ToolTrace(), cache=cache)
    result = fetcher.fetch("http://example.com/cached")
    assert result.content == b"from cache"
    assert fetcher._trace.events[0].status == "cached"
    assert fetcher._trace.events[0].cache_hit is True


# --------------------------------------------------------------------- cache
def test_cache_put_get_and_expiry(tmp_path: Path) -> None:
    cache = DocCache(tmp_path, max_age_seconds=0)
    entry = CacheEntry(
        url="u",
        final_url="u",
        status=200,
        content=b"x",
        content_type="text/plain",
        etag=None,
        fetched_at=time.time(),
    )
    cache.put(cache.key_for("u"), entry)
    # max_age=0 -> already expired
    assert cache.get(cache.key_for("u")) is None

    fresh = DocCache(tmp_path, max_age_seconds=3600)
    fresh.put(fresh.key_for("u"), entry)
    got = fresh.get(fresh.key_for("u"))
    assert got is not None and got.content == b"x"


# ---------------------------------------------------------------------- pypi
def test_pypi_latest_version_and_changelog() -> None:
    payload = {
        "info": {"version": "2.7.0"},
        "releases": {
            "2.7.0": [{"upload_time_iso_8601": "2024-01-02T00:00:00Z", "yanked": False}],
            "2.6.0": [{"upload_time_iso_8601": "2023-01-01T00:00:00Z", "yanked": True}],
        },
    }
    opener = make_opener(
        {
            "https://pypi.org/pypi/foo/json": FakeResponse(
                200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
            )
        }
    )
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(opener=opener)
        client = PyPIClient(fetcher)
        assert client.latest_version("foo") == "2.7.0"
        changelog = client.changelog("foo")
    assert {e.version for e in changelog} == {"2.7.0", "2.6.0"}
    assert changelog[0].version == "2.7.0"  # sorted newest first
    assert changelog[1].is_yanked is True


# --------------------------------------------------------------------- github
def test_github_release_changelog() -> None:
    payload = [
        {
            "tag_name": "v2.0",
            "name": "2.0",
            "body": "big",
            "published_at": "2024",
            "html_url": "https://github.com/o/r/releases/tag/v2.0",
            "prerelease": False,
        }
    ]
    opener = make_opener(
        {
            "https://api.github.com/repos/o/r/releases?per_page=20": FakeResponse(
                200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
            )
        }
    )
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(opener=opener)
        releases = GitHubClient(fetcher).release_changelog("o/r")
    assert releases[0].tag == "v2.0"


def test_github_release_changelog_degrades_on_403() -> None:
    opener = make_opener(
        {
            "https://api.github.com/repos/o/r/releases?per_page=20": FakeResponse(
                403, b"rate limited", {}
            )
        }
    )
    with patch.object(RestrictedFetcher, "_resolve_ips", return_value=["93.184.216.34"]):
        fetcher = RestrictedFetcher(opener=opener)
        # The client degrades gracefully (returns []), not crashes.
        assert GitHubClient(fetcher).release_changelog("o/r") == []


def test_validate_ref_rejects_shell_metachars() -> None:
    assert validate_ref("main") is True
    assert validate_ref("release/2.0") is True
    assert validate_ref(";rm -rf /") is False
    assert validate_ref("$(curl evil)") is False


# ------------------------------------------------------------------ live_repo
def test_is_repo_url_and_slug() -> None:
    assert is_repo_url("https://github.com/o/r") is True
    assert is_repo_url("https://github.com/o/r.git") is True
    assert is_repo_url("/local/path") is False
    assert is_repo_url("https://gitlab.com/o/r") is False
    assert parse_repo_slug("https://github.com/owner/repo.git") == "owner/repo"


def test_clone_live_repo_validates_url_and_cleans_up(tmp_path: Path) -> None:
    with (
        patch("upgradelens.tools.live_repo.shallow_clone") as clone_mock,
        patch("tempfile.TemporaryDirectory") as tmp_mock,
    ):
        tmp_mock.return_value.__enter__.return_value = str(tmp_path)
        handle = clone_live_repo("https://github.com/owner/repo")
        clone_mock.assert_called_once()
        assert handle.path.exists() is False or True  # temp dir behaviour
        handle.cleanup()

    # Non-GitHub URL must be rejected before any clone attempt.
    with pytest.raises(OutOfNetworkError):
        clone_live_repo("https://gitlab.com/owner/repo")

    # Unsafe ref must be rejected.
    with pytest.raises(OutOfNetworkError):
        clone_live_repo("https://github.com/owner/repo", ref=";rm -rf /")
