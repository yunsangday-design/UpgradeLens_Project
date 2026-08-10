"""The capability pack contract.

A :class:`CapabilityPack` is an *optional, additive* ability an upgrade
assessment may use. It deliberately does **not** own facts: no retrieved
document, no curated retrieval query, no risk hint. Those live in the shared
RAG corpus. A pack only contributes mechanical abilities, and every hook below
has a safe default so the caller need not know which abilities a given pack
implements.

The main pipeline must degrade gracefully when no pack is present -- a missing
pack must never block or downgrade the corpus-driven assessment.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any

from upgradelens.domain.skill import PatchRule


@dataclass(frozen=True)
class CapabilityPack(ABC):
    """An optional ability a dependency assessment may draw on.

    Concrete packs override only the hooks they provide. Every hook returns a
    safe default (``None`` or empty) so a pack can be threaded through the
    pipeline without the caller caring which abilities it implements.
    """

    id: str
    name: str = ""

    def applies_to(self, *, package: str, source_version: str, target_version: str) -> bool:
        """Whether this pack is relevant to the given upgrade.

        Permissive by default: callers narrow the pack set before invoking any
        hook, so a pack need not filter on its own.
        """
        return True

    # -- optional hooks (safe no-ops / empties by default) ----------------- #
    def patch_rules(self) -> list[PatchRule]:
        """Mechanical rewrite rules this pack can apply to the repo."""
        return []

    def allow_patch_draft(self) -> bool:
        """Whether the pack permits drafting a Unified Diff against the repo."""
        return False

    def parse_manifest(self, *, text: str, filename: str) -> dict[str, Any] | None:
        """Best-effort manifest parser (e.g. ``pyproject.toml`` / ``requirements``)."""
        return None

    def validate(self, *, report: Any, bundle: Any) -> list[Any] | None:
        """Extra validation beyond the evidence-driven verifier. ``None`` = no opinion."""
        return None

    def recommend_tests(self, *, bundle: Any) -> list[str] | None:
        """Dependency-specific test commands to suggest. ``None`` = no suggestion."""
        return None
