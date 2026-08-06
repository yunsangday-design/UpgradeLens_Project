"""PyPI client: latest version, release metadata, and changelog.

All HTTP goes through :class:`RestrictedFetcher`, so every call is traced and
subject to the same SSRF/limit guards as any other document fetch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from upgradelens.tools.fetcher import RestrictedFetcher

_PYPI_BASE = "https://pypi.org/pypi"


@dataclass
class ChangelogEntry:
    """One published release of a distribution on PyPI."""

    version: str
    released: str | None
    summary: str
    url: str
    is_prerelease: bool
    is_yanked: bool


class PyPIClient:
    """Thin, traced wrapper over the PyPI JSON API."""

    def __init__(self, fetcher: RestrictedFetcher) -> None:
        self._fetcher = fetcher

    def _json(self, url: str) -> dict[str, Any]:
        result = self._fetcher.fetch(url)
        data: dict[str, Any] = json.loads(result.content.decode("utf-8", "replace"))
        return data

    def latest_version(self, name: str) -> str:
        data = self._json(f"{_PYPI_BASE}/{name}/json")
        return str(data["info"]["version"])

    def changelog(self, name: str, since_version: str | None = None) -> list[ChangelogEntry]:
        """Return published releases, newest first.

        ``since_version`` is accepted for API symmetry but PyPI does not expose
        a filtered endpoint; filtering is left to the caller (or the skill) via
        the returned entries. A release marked yanked is still returned so the
        Verifier can flag it.
        """
        data = self._json(f"{_PYPI_BASE}/{name}/json")
        releases = data.get("releases", {})
        entries: list[ChangelogEntry] = []
        for version, files in releases.items():
            meta = files[0] if files else {}
            uploaded = meta.get("upload_time_iso_8601") or meta.get("upload_time")
            entries.append(
                ChangelogEntry(
                    version=str(version),
                    released=uploaded,
                    summary="",
                    url=f"{_PYPI_BASE}/{name}/{version}/json",
                    is_prerelease=bool(meta.get("is_prerelease", False)),
                    is_yanked=bool(meta.get("yanked", False)),
                )
            )
        entries.sort(key=lambda e: e.released or "", reverse=True)
        return entries
