"""Optional capability packs (stage 5 / B5).

The shared RAG corpus owns the *facts*; a capability pack owns optional,
mechanical *abilities* -- manifest parsing, extra validation, test
recommendation, and safe mechanical transformations. The main assessment path
never depends on a pack being present: every hook has a safe default, and a
missing pack costs no capability the corpus-backed pipeline cannot already
provide.

The first concrete pack is :class:`TransformationPack`, which carries the
rewrite rules that used to live on a Skill Pack so the patch generator no longer
imports a fact-bearing Skill merely to read its abilities.
"""

from __future__ import annotations

from upgradelens.capabilities.base import CapabilityPack
from upgradelens.capabilities.registry import CapabilityRegistry
from upgradelens.capabilities.transformation import TransformationPack

__all__ = ["CapabilityPack", "TransformationPack", "CapabilityRegistry"]
