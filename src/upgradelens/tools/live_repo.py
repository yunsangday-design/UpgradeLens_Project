"""Live repository handling: validate a GitHub URL, shallow-clone to a temp dir.

This is the "analyse a real public repo" half of stage 7. The clone is hard
restricted: only ``https://github.com/<owner>/<repo>`` URLs are accepted, the
branch/tag is validated against a strict pattern before it ever reaches a
subprocess, and the checkout lives in a temp dir that we clean up explicitly.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from upgradelens.tools.errors import OutOfNetworkError, ToolError
from upgradelens.tools.github import shallow_clone, validate_ref

_GITHUB_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:\.git)?/?$")


def is_repo_url(value: str) -> bool:
    """True if ``value`` is an http(s) URL that *might* be a git repo (not a
    local filesystem path)."""
    if not value.startswith("http://") and not value.startswith("https://"):
        return False
    return _GITHUB_RE.match(value) is not None


def parse_repo_slug(url: str) -> str:
    """Extract ``owner/repo`` from a GitHub URL."""
    path = urlparse(url).path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise OutOfNetworkError(f"cannot parse owner/repo from {url}")
    return f"{parts[0]}/{parts[1]}"


@dataclass
class LiveRepoHandle:
    """A cloned repository plus its temp dir for cleanup."""

    path: Path
    _tmp: tempfile.TemporaryDirectory[str] | None

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def clone_live_repo(
    url: str,
    ref: str | None = None,
    *,
    workdir: Path | None = None,
) -> LiveRepoHandle:
    """Clone ``url`` (optionally at ``ref``) into a fresh temp dir.

    Raises :class:`OutOfNetworkError` for non-GitHub URLs or unsafe refs, and
    :class:`ToolError` if the clone itself fails. The temp dir is always removed
    on failure; on success it must be cleaned up by the caller via
    :meth:`LiveRepoHandle.cleanup`.
    """
    if not is_repo_url(url):
        raise OutOfNetworkError(f"not a GitHub repository URL: {url}")
    slug = parse_repo_slug(url)
    ref = ref or "main"
    if not validate_ref(ref):
        raise OutOfNetworkError(f"unsafe git ref rejected: {ref!r}")

    tmp = tempfile.TemporaryDirectory(prefix="ul-live-", dir=workdir)
    dest = Path(tmp.name) / slug.replace("/", "__")
    try:
        shallow_clone(url, ref, str(dest))
    except ToolError:
        tmp.cleanup()
        raise
    return LiveRepoHandle(path=dest, _tmp=tmp)
