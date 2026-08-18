"""Collect git diffs as structured :class:`ChangeSet` objects (plan stage S3).

Wraps ``git diff`` and pipes the output through :func:`parse_unified_diff`. Fully
offline (works on any local checkout) and deterministic for a given ref.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from upgradelens.change.diff import parse_unified_diff
from upgradelens.change.models import ChangeSet

__all__ = ["collect_git_diff", "collect_workspace_diff"]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def collect_git_diff(
    repo: Path, refspec: str = "HEAD~1..HEAD", *, unified: int = 3
) -> ChangeSet:
    """Parse the diff for ``refspec`` (default: the last commit)."""
    out = _git(
        repo,
        "diff",
        f"--unified={unified}",
        "--no-color",
        "-M",
        "--no-ext-diff",
        refspec,
    )
    return parse_unified_diff(out)


def collect_workspace_diff(repo: Path, *, unified: int = 3) -> ChangeSet:
    """Parse uncommitted changes: both unstaged and staged working-tree diffs."""
    unstaged = _git(
        repo, "diff", f"--unified={unified}", "--no-color", "-M", "--no-ext-diff"
    )
    staged = _git(
        repo,
        "diff",
        "--cached",
        f"--unified={unified}",
        "--no-color",
        "-M",
        "--no-ext-diff",
    )
    return parse_unified_diff(unstaged + "\n" + staged)
