"""Stage 8 patch-draft generation (plan section 2.3)."""

from __future__ import annotations

from upgradelens.patch.generator import generate_patch_draft
from upgradelens.patch.models import PatchDraft, PatchFileDiff, PatchHunk

__all__ = [
    "generate_patch_draft",
    "PatchDraft",
    "PatchFileDiff",
    "PatchHunk",
]
