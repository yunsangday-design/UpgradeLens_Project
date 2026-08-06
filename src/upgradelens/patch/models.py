"""Patch draft data models (plan section 2.3: high-certainty Unified Diff draft).

A :class:`PatchDraft` is a *proposal*. UpgradeLens never writes it back to the
repository (plan section 1.5 / 2.5: "不直接修改仓库"). The diff text is produced
for a human or a coding agent to review, so it is fully deterministic and
self-contained.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PatchHunk(BaseModel):
    """A single unified-diff hunk for one file."""

    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # Lines with a leading ' ', '-' or '+' (no prefix marker stored separately).
    body: list[str]


class PatchFileDiff(BaseModel):
    """All hunks that touch one file, plus the rendered unified-diff text."""

    path: str
    hunks: list[PatchHunk] = Field(default_factory=list)
    diff_text: str = ""

    def merged_body(self) -> str:
        return "\n".join(self.hunks[0].body) if self.hunks else ""


class PatchDraft(BaseModel):
    """A complete, review-ready patch proposal for one dependency upgrade.

    ``applied_rules`` lists rule ids that produced a hunk; ``skipped_rules``
    lists rules that were eligible in principle but deliberately not applied
    (e.g. they need a quality model we did not run, or their risk level is too
    high for automatic drafting).
    """

    dependency: str = ""
    target_version_spec: str = ""
    skill_id: str = ""
    allow_patch_draft: bool = False
    quality_model_available: bool = False
    files: list[PatchFileDiff] = Field(default_factory=list)
    applied_rules: list[str] = Field(default_factory=list)
    skipped_rules: list[str] = Field(default_factory=list)
    notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.files

    def to_unified_diff(self) -> str:
        """Render the full ``upgrade.patch`` text (empty string when no hunks)."""
        if not self.files:
            return ""
        parts: list[str] = []
        for fd in self.files:
            if not fd.diff_text:
                continue
            parts.append(fd.diff_text)
        return "\n".join(parts).rstrip("\n") + "\n" if parts else ""
