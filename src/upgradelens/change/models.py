"""Change-set data model (plan stage S3).

A :class:`ChangeSet` is the deterministic, structured view of a git diff: which files
changed, how (added/modified/deleted/renamed/binary), the hunks and line counts. It
is produced entirely by parsing text -- no model, no network -- so it is the safe
foundation S4 (PR review), S6 (issue repair) and the impact analyzer build on.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChangeLabel(StrEnum):
    """How a single file changed."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    BINARY = "binary"


class ChangeHunk(BaseModel):
    """One contiguous changed region inside a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # Raw hunk lines (including the leading "+"/"-"/" " markers) for traceability.
    lines: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0


class FileChange(BaseModel):
    """One file's change within a :class:`ChangeSet`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    label: ChangeLabel
    # For renames, the previous path (``path`` is the new path).
    old_path: str | None = None
    hunks: list[ChangeHunk] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    language: str = ""


class DiffStat(BaseModel):
    """Aggregate counts for a change set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    files_by_label: dict[str, int] = Field(default_factory=dict)


class ChangeSet(BaseModel):
    """A parsed, structured git diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_ref: str = ""
    head_ref: str = ""
    commit: str = ""
    files: list[FileChange] = Field(default_factory=list)
    stat: DiffStat = Field(default_factory=DiffStat)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def total_changes(self) -> int:
        return self.stat.additions + self.stat.deletions
