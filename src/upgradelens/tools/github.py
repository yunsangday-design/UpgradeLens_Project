"""GitHub client: release/changelog retrieval, clone, and comment posting.

Three capabilities live here:

* ``release_changelog`` talks to the public GitHub REST API (no auth needed for
  public repos). When the API is rate-limited or forbidden we *degrade* -- the
  plan explicitly tolerates "no changelog" rather than a crashed run.
* ``shallow_clone`` shells out to ``git`` with a hardened, validated ref. It does
  **not** use :class:`RestrictedFetcher` (git does its own network), but the
  URL and branch/tag are validated beforehand so no shell metacharacter can
  reach the subprocess.
* ``post_issue_comment`` / ``comment_pr`` -- the "close the loop" step: post an
  assessment report back to a PR or issue as a comment. It is the only write
  here and it reuses the read path's SSRF guard and trace, so posting stays
  inside the same security model (no ad-hoc HTTP, token never logged).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from upgradelens.tools.errors import ApiDegradedError, ToolError
from upgradelens.tools.fetcher import RestrictedFetcher

_GITHUB_API = "https://api.github.com"

#: Refs we will accept for a shallow clone. Anything shell-shaped is rejected.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: ``owner/repo`` -- the slug shape GitHub uses for its API paths.
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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

    def post_issue_comment(
        self,
        repo_slug: str,
        issue_number: int,
        body: str,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Post ``body`` as a comment on issue/PR ``issue_number`` of ``repo_slug``.

        Returns the GitHub API payload (with ``html_url``/``id``). Raises
        :class:`ToolError` on any failure -- callers should degrade gracefully
        rather than abort the whole assessment.

        Security: the target host is checked by the same SSRF guard the read
        path uses, and the token is sent only as an ``Authorization`` header and
        is never recorded in the trace.
        """
        if not _SLUG_RE.match(repo_slug):
            raise ToolError(f"Invalid GitHub repo slug: {repo_slug!r}")
        if not body.strip():
            raise ToolError("Refusing to post an empty comment")
        url = f"{_GITHUB_API}/repos/{repo_slug}/issues/{int(issue_number)}/comments"
        if not self._fetcher.is_url_allowed(url):
            raise ToolError("Refused: GitHub API host is not allowed by the HTTP policy")
        payload = json.dumps({"body": body}).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "upgradelens",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        trace = self._fetcher.trace
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                status = resp.getcode()
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            elapsed = (time.monotonic() - started) * 1000
            snippet = exc.read().decode("utf-8", "replace")[:400]
            trace.record(
                tool="github.comment",
                target=url,
                status="error",
                http_status=exc.code,
                bytes_=len(payload),
                latency_ms=elapsed,
                error=f"HTTP {exc.code}: {snippet}",
            )
            raise ToolError(f"GitHub API returned {exc.code}: {snippet}") from exc
        except OSError as exc:
            elapsed = (time.monotonic() - started) * 1000
            trace.record(
                tool="github.comment",
                target=url,
                status="error",
                bytes_=len(payload),
                latency_ms=elapsed,
                error=str(exc),
            )
            raise ToolError(f"GitHub API request failed: {exc}") from exc
        elapsed = (time.monotonic() - started) * 1000
        trace.record(
            tool="github.comment",
            target=url,
            status="ok",
            http_status=status,
            bytes_=len(payload),
            latency_ms=elapsed,
        )
        try:
            data: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"html_url": None, "id": None}
        return data

    def comment_pr(
        self,
        repo_slug: str,
        pr_number: int,
        body: str,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Post ``body`` as a comment on pull request ``pr_number``.

        Pull requests are issues in GitHub's API, so this delegates to
        :meth:`post_issue_comment`.
        """
        return self.post_issue_comment(repo_slug, pr_number, body, token=token)


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
