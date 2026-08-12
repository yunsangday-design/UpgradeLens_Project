"""Restricted URL fetcher -- the SSRF-safe, cache-aware, traced HTTP client.

This is the *only* component that performs outbound HTTP for document
acquisition in stage 7. It is deliberately narrow:

* it rejects any host that resolves to a private/internal address (SSRF guard);
* it honours an optional allow-list;
* it enforces a byte ceiling, a redirect budget, and a timeout;
* it throttles per host;
* every call is recorded in a :class:`ToolTrace`, and cache hits are
  distinguished from live fetches.

It relies on the standard library only (``urllib``), so tests can inject a fake
opener and never touch the network.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from upgradelens.tools.cache import CacheEntry, DocCache
from upgradelens.tools.errors import (
    FetchTimeoutError,
    HttpError,
    OutOfNetworkError,
    TooLargeError,
    ToolError,
    TooManyRedirectsError,
)
from upgradelens.tools.trace import ToolTrace

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_USER_AGENT = "upgradelens/0.1 (+https://github.com/upgradelens)"

#: Default blocked ranges: RFC1918, loopback, link-local, and the IPv6
#: equivalents. Resolving to any of these aborts the fetch.
DEFAULT_BLOCKED_NETWORKS: tuple[_Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


@dataclass
class FetchConfig:
    """Tunable limits for :class:`RestrictedFetcher`."""

    allow_hosts: frozenset[str] | None = None
    max_bytes: int = 5_000_000
    timeout: float = 15.0
    max_redirects: int = 5
    min_interval_per_host: float = 0.1
    blocked_networks: tuple[_Network, ...] = DEFAULT_BLOCKED_NETWORKS


@dataclass
class FetchResult:
    """A successfully fetched document."""

    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str
    etag: str | None
    headers: dict[str, str] = field(default_factory=dict)


class _Response(Protocol):
    """The minimal surface we need from an ``urllib`` response."""

    def getcode(self) -> int: ...

    @property
    def headers(self) -> Any: ...

    def read(self, n: int = ...) -> bytes: ...

    def geturl(self) -> str: ...

    def close(self) -> None: ...


Opener = Callable[[urllib.request.Request, float], _Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from auto-following redirects; we handle them ourselves so
    the redirect budget and per-hop SSRF checks are enforced."""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:  # noqa: D401
        return None


_DEFAULT_OPENER = urllib.request.build_opener(_NoRedirect)


def _default_open(req: urllib.request.Request, timeout: float) -> _Response:
    return _DEFAULT_OPENER.open(req, timeout=timeout)  # type: ignore[no-any-return]


class RestrictedFetcher:
    """SSRF-safe, cache-aware, traced HTTP client."""

    def __init__(
        self,
        config: FetchConfig | None = None,
        *,
        trace: ToolTrace | None = None,
        cache: DocCache | None = None,
        opener: Opener | None = None,
    ) -> None:
        self._config = config or FetchConfig()
        self._trace = trace or ToolTrace()
        self._cache = cache
        self._opener = opener or _default_open
        self._last_call: dict[str, float] = {}
        # Step 13, #3.2: serialises trace writes while the network I/O runs
        # outside the lock, so parallel fetches overlap (the cache guards its
        # own I/O via DocCache._lock).
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ guards
    def _resolve_ips(self, host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise OutOfNetworkError(f"cannot resolve host: {host}") from exc
        return sorted(str(info[4][0]) for info in infos)

    def _host_is_internal(self, host: str) -> bool:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            ips = [ipaddress.ip_address(a) for a in self._resolve_ips(host)]
        else:
            ips = [literal]
        for ip in ips:
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return True
            for net in self._config.blocked_networks:
                if ip in net:
                    return True
        return False

    def _check_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise OutOfNetworkError(f"rejected scheme for {url}")
        host = parsed.hostname or ""
        if not host:
            raise OutOfNetworkError(f"missing host for {url}")
        if self._config.allow_hosts is not None:
            allowed = host in self._config.allow_hosts or any(
                host == h or host.endswith("." + h) for h in self._config.allow_hosts
            )
            if not allowed:
                raise OutOfNetworkError(f"host not on allow-list: {host}")
        if self._host_is_internal(host):
            raise OutOfNetworkError(f"host resolves to an internal address: {host}")

    @property
    def trace(self) -> ToolTrace:
        """The trace this fetcher records into (handy for mutation callers)."""
        return self._trace

    def is_url_allowed(self, url: str) -> bool:
        """Return ``True`` if ``url`` passes the SSRF/host guard.

        Unlike :meth:`_check_url`, this never raises -- it is used by mutation
        helpers (e.g. posting a GitHub comment) that must decide up front
        whether a target host is permitted before opening a connection.
        """
        try:
            self._check_url(url)
        except ToolError:
            return False
        return True

    def _throttle(self, host: str) -> None:
        interval = self._config.min_interval_per_host
        if interval <= 0:
            return
        now = time.monotonic()
        last = self._last_call.get(host)
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    # ------------------------------------------------------------------- fetch
    def fetch(
        self, url: str, *, refresh: bool = False, params: dict[str, Any] | None = None
    ) -> FetchResult:
        """Fetch ``url`` through the cache/fetch pipeline.

        Cache-first: when a fresh entry exists and ``refresh`` is false, it is
        returned with ``cache_hit`` recorded. Otherwise the network is used and
        the result is cached. ``self._lock`` serialises the trace writes so that
        several threads may call ``fetch`` in parallel (Step 13, #3.2); the cache
        guards its own I/O, and the network request runs outside the lock.
        """
        if self._cache is not None and not refresh:
            key = self._cache.key_for(url)
            hit = self._cache.get(key)
            if hit is not None:
                with self._lock:
                    self._trace.record(
                        tool="fetcher.cache",
                        target=url,
                        status="cached",
                        http_status=hit.status,
                        bytes_=len(hit.content),
                        cache_hit=True,
                        params=params,
                    )
                return FetchResult(
                    url=url,
                    final_url=hit.final_url,
                    status=hit.status,
                    content=hit.content,
                    content_type=hit.content_type,
                    etag=hit.etag,
                    headers={},
                )

        self._check_url(url)
        host = urllib.parse.urlparse(url).hostname or ""
        self._throttle(host)
        start = time.monotonic()
        try:
            result = self._fetch_with_redirects(url, 0)
        except ToolError as exc:
            with self._lock:
                self._trace.record(
                    tool="fetcher",
                    target=url,
                    status="error",
                    error=str(exc),
                    latency_ms=(time.monotonic() - start) * 1000,
                    params=params,
                )
            raise
        except (TimeoutError, urllib.error.URLError) as exc:  # pragma: no cover
            with self._lock:
                self._trace.record(
                    tool="fetcher",
                    target=url,
                    status="error",
                    error=str(exc),
                    latency_ms=(time.monotonic() - start) * 1000,
                    params=params,
                )
            raise

        latency = (time.monotonic() - start) * 1000
        if self._cache is not None:
            self._cache.put(
                self._cache.key_for(url),
                CacheEntry(
                    url=url,
                    final_url=result.final_url,
                    status=result.status,
                    content=result.content,
                    content_type=result.content_type,
                    etag=result.etag,
                    fetched_at=time.time(),
                ),
            )
        with self._lock:
            self._trace.record(
                tool="fetcher",
                target=url,
                status="ok",
                http_status=result.status,
                bytes_=len(result.content),
                latency_ms=latency,
                params=params,
            )
        return result

    def _fetch_with_redirects(self, url: str, depth: int) -> FetchResult:
        if depth > self._config.max_redirects:
            raise TooManyRedirectsError(
                f"more than {self._config.max_redirects} redirects from {url}"
            )
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            resp = self._opener(req, self._config.timeout)
        except urllib.error.HTTPError as exc:
            # With the _NoRedirect handler, urllib surfaces 3xx responses as
            # HTTPError. We still need to follow them manually, so treat any 3xx
            # as a redirect response instead of a hard error.
            if 300 <= exc.code < 400:
                location = exc.headers.get("Location") if exc.headers.get("Location") else None
                if location:
                    next_url = urllib.parse.urljoin(url, location)
                    self._check_url(next_url)
                    return self._fetch_with_redirects(next_url, depth + 1)
            raise HttpError(status=exc.code, url=url) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
                raise FetchTimeoutError(str(reason)) from exc
            raise ToolError(f"network error: {reason}") from exc
        except TimeoutError as exc:
            raise FetchTimeoutError(str(exc)) from exc

        code = resp.getcode()
        location = resp.headers.get("Location") if resp.headers.get("Location") else None
        if 300 <= code < 400 and location:
            resp.close()
            next_url = urllib.parse.urljoin(url, location)
            self._check_url(next_url)
            return self._fetch_with_redirects(next_url, depth + 1)
        if code >= 400:
            resp.close()
            raise HttpError(status=code, url=url)

        content, etag = self._read_body(resp)
        final_url = resp.geturl() if hasattr(resp, "geturl") else url
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        return FetchResult(
            url=url,
            final_url=final_url,
            status=code,
            content=content,
            content_type=ctype,
            etag=etag,
            headers=dict(resp.headers.items()),
        )

    def _read_body(self, resp: _Response) -> tuple[bytes, str | None]:
        etag = resp.headers.get("ETag")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self._config.max_bytes:
                resp.close()
                raise TooLargeError(
                    f"response exceeded {self._config.max_bytes} bytes for {resp.geturl()}"
                )
            chunks.append(chunk)
        resp.close()
        return b"".join(chunks), etag
