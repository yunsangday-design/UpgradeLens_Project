"""Cache-first store for fetched documents (stage 7).

The whole point of the live path is to *not* re-hit the network when we already
have a fresh copy. This store is a tiny on-disk cache: each entry is the fetched
bytes plus its metadata (final URL, content type, etag, fetch time). The
fetcher consults it before any network call and falls back to it when the
upstream is unreachable -- so a flaky network degrades to "served from cache"
rather than "lost the run" (acceptance criterion 3).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    """A single cached fetch."""

    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str
    etag: str | None
    fetched_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "content": self.content.decode("utf-8", "replace"),
            "content_type": self.content_type,
            "etag": self.etag,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheEntry:
        return cls(
            url=data["url"],
            final_url=data["final_url"],
            status=int(data["status"]),
            # The body is stored in a sidecar .bin file; ``content`` here is the
            # throwaway placeholder written into the meta JSON (see ``put``).
            content=data.get("content", "").encode("utf-8"),
            content_type=data["content_type"],
            etag=data.get("etag"),
            fetched_at=float(data["fetched_at"]),
        )


class DocCache:
    """A directory-backed, cache-first store keyed by a stable key string."""

    def __init__(self, cache_dir: Path, max_age_seconds: int = 7 * 24 * 3600) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_age = max_age_seconds
        # Step 13, #3.2: guard file I/O so the cache can be shared across the
        # worker threads that fetch discovered sources in parallel.
        self._lock = threading.Lock()

    @staticmethod
    def key_for(*parts: str) -> str:
        """Build a stable cache key from one or more identity parts."""
        joined = "\0".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]

    def _meta_path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _content_path(self, key: str) -> Path:
        return self._dir / f"{key}.bin"

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            meta_path = self._meta_path(key)
            if not meta_path.exists():
                return None
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                content = self._content_path(key).read_bytes()
            except (OSError, ValueError):
                return None
            if time.time() - float(meta.get("fetched_at", 0)) > self._max_age:
                return None
            entry = CacheEntry.from_dict(meta)
            entry.content = content
            return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        with self._lock:
            self._meta_path(key).write_text(
                json.dumps(
                    {k: v for k, v in entry.to_dict().items() if k != "content"},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._content_path(key).write_bytes(entry.content)

    def clear(self) -> None:
        for path in self._dir.glob("*.json"):
            path.unlink(missing_ok=True)
        for path in self._dir.glob("*.bin"):
            path.unlink(missing_ok=True)
