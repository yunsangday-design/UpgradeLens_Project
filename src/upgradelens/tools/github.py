"""GitHub client: release/changelog retrieval and restricted shallow clone.

Two very different capabilities live here:

* ``release_changelog`` talks to the public GitHub REST API (no auth needed for
  public repos). When the API is rate-limited or forbidden we *degrade* -- the
  plan explicitly tolerates "no changelog" rather than a crashed run.
* ``shallow_clone`` shells out to ``git`` with a hardened, validated ref. It does
  **not** use :class:`RestrictedFetcher` (git does its own network), but the
  URL and branch/tag are validated beforehand so no shell metacharacter can
  reach the subprocess.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from upgradelens.tools.errors import ApiDegradedError, ToolError
from upgradelens.tools.fetcher import RestrictedFetcher

_GITHUB_API = "https://api.github.com"

#: Refs we will accept for a shallow clone. Anything shell-shaped is rejected.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass
class Release:
    """A GitHub release."""

    tag: str
    name: str
    body: str
    published_at: str | None
    url: str
    prerelease: bool


class GitHubClient:
    """Traced access to the GitHub REST API."""

    def __init__(self, fetcher: RestrictedFetcher) -> None:
        self._fetcher = fetcher

    def release_changelog(self, repo_slug: str) -> list[Release]:
        """Return recent releases, newest first.

        Degrades gracefully: on any tool-layer failure (rate limit, timeout,
        network) an empty list is returned and the failure is already recorded
        in the trace -- the caller can still proceed using other evidence.
        """
        try:
            result = self._fetcher.fetch(f"{_GITHUB_API}/repos/{repo_slug}/releases?per_page=20")
        except ApiDegradedError:
            return []
        except ToolError:
            return []
        try:
            data = json.loads(result.content.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        out: list[Release] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            out.append(
                Release(
                    tag=str(item.get("tag_name", "")),
                    name=str(item.get("name", "")),
                    body=str(item.get("body", "")),
                    published_at=item.get("published_at"),
                    url=str(item.get("html_url", "")),
                    prerelease=bool(item.get("prerelease")),
                )
            )
        return out


def validate_ref(ref: str) -> bool:
    """Return True if ``ref`` is safe to pass to ``git clone --branch``."""
    return bool(ref) and _SAFE_REF_RE.match(ref) is not None


def shallow_clone(url: str, ref: str, dest: str, *, timeout: float = 120.0) -> None:
    """Clone ``url`` at ``ref`` into ``dest`` with depth 1.

    Raises :class:`ToolError` on a non-zero exit. The URL and ref must already
    have been validated by the caller (see :func:`validate_ref`).
    """
    if not validate_ref(ref):
        raise ToolError(f"unsafe git ref rejected: {ref!r}")
    cmd = ["git", "clone", "--depth", "1", "--branch", ref, url, dest]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ToolError(
            f"git clone failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
