"""Parse a unified diff into a structured :class:`ChangeSet` (plan stage S3).

This is pure text parsing -- no model, no network. The same parser is used for both
git-produced diffs (``change/git.py``) and reviewer-provided diff snippets, so the
rest of the pipeline can rely on a single deterministic representation.
"""

from __future__ import annotations

import re
from typing import Any

from upgradelens.change.models import ChangeHunk, ChangeLabel, ChangeSet, DiffStat, FileChange

__all__ = ["parse_unified_diff", "is_safe_path", "language_for_path"]

_DIFF_HEADER = re.compile(r"diff --git a/(.*) b/(.*)$")
_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def language_for_path(path: str) -> str:
    """Best-effort language guess from a file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "go": "go",
        "java": "java",
        "rs": "rust",
        "rb": "ruby",
        "kt": "kotlin",
        "c": "c",
        "cpp": "cpp",
        "h": "c",
        "cs": "csharp",
        "php": "php",
        "swift": "swift",
    }.get(ext, ext or "unknown")


def is_safe_path(path: str) -> bool:
    """Reject absolute paths, null bytes and ``..`` traversal."""
    if not path:
        return False
    if "\x00" in path:
        return False
    if path.startswith("/") or path.startswith("\\"):
        return False
    parts = path.replace("\\", "/").split("/")
    return ".." not in parts


def _strip_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    if path in ("/dev/null", "dev/null"):
        return ""
    return path


def _parse_hunk(raw: list[str], start: int) -> tuple[ChangeHunk, int]:
    m = _HUNK_HEADER.match(raw[start])
    if not m:
        # Not a real hunk header; bail out consuming only this line.
        return (
            ChangeHunk(
                old_start=0, old_count=0, new_start=0, new_count=0,
                lines=[raw[start]],
            ),
            start + 1,
        )
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) else 1
    lines = [raw[start]]
    additions = 0
    deletions = 0
    i = start + 1
    n = len(raw)
    while i < n and not raw[i].startswith(("@@", "diff --git ")):
        line = raw[i]
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
        lines.append(line)
        i += 1
    hunk = ChangeHunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        lines=lines,
        additions=additions,
        deletions=deletions,
    )
    return hunk, i


def _parse_file_block(raw: list[str], start: int) -> dict[str, Any]:
    m = _DIFF_HEADER.match(raw[start])
    old_path = m.group(1) if m else ""
    new_path = m.group(2) if m else ""
    label = ChangeLabel.MODIFIED
    rename_old: str | None = None
    old_path_final = _strip_prefix(old_path)
    new_path_final = _strip_prefix(new_path)

    i = start + 1
    n = len(raw)
    hunks: list[ChangeHunk] = []
    while i < n and not raw[i].startswith("diff --git "):
        line = raw[i]
        if line.startswith("new file mode"):
            label = ChangeLabel.ADDED
        elif line.startswith("deleted file mode"):
            label = ChangeLabel.DELETED
        elif line.startswith("rename from "):
            rename_old = line[len("rename from ") :].strip()
        elif line.startswith("rename to "):
            new_path_final = line[len("rename to ") :].strip()
        elif line.startswith("Binary files"):
            label = ChangeLabel.BINARY
        elif line.startswith("@@"):
            hunk, i = _parse_hunk(raw, i)
            hunks.append(hunk)
            continue
        i += 1

    if rename_old is not None or label is ChangeLabel.RENAMED:
        label = ChangeLabel.RENAMED

    path = new_path_final or old_path_final
    old_path_out = old_path_final if label is ChangeLabel.RENAMED else None
    file = FileChange(
        path=path,
        label=label,
        old_path=old_path_out,
        hunks=hunks,
        additions=sum(h.additions for h in hunks),
        deletions=sum(h.deletions for h in hunks),
        language=language_for_path(path),
    )
    return {"file": file, "next": i}


def parse_unified_diff(text: str) -> ChangeSet:
    """Parse a unified diff (as produced by ``git diff``) into a ChangeSet."""
    raw = text.splitlines()
    files: list[FileChange] = []
    additions = 0
    deletions = 0
    files_by_label: dict[str, int] = {}
    i = 0
    n = len(raw)
    while i < n:
        if raw[i].startswith("diff --git "):
            block = _parse_file_block(raw, i)
            i = block["next"]
            fc = block["file"]
            if not is_safe_path(fc.path):
                continue
            files.append(fc)
            additions += fc.additions
            deletions += fc.deletions
            files_by_label[fc.label.value] = files_by_label.get(fc.label.value, 0) + 1
        else:
            i += 1
    stat = DiffStat(
        files_changed=len(files),
        additions=additions,
        deletions=deletions,
        files_by_label=files_by_label,
    )
    return ChangeSet(files=files, stat=stat)
