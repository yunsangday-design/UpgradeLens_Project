"""The transformation capability: safe, mechanical rewrite rules.

This is the first concrete :class:`~upgradelens.capabilities.base.CapabilityPack`.
It carries the rewrite rules that previously lived on a Skill Pack's
``patch_rules`` field, so the patch generator no longer imports a fact-bearing
Skill merely to read its abilities.

A :class:`TransformationPack` can be built directly, or adapted from a legacy
Skill Pack during the compatibility window via :meth:`from_skill`. The adapter
keeps the skill's *facts* in the RAG corpus while only its rewrite rules leave
through the capability path. A generic (fact-only) skill yields an empty,
non-drafting pack.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from upgradelens.capabilities.base import CapabilityPack
from upgradelens.domain.skill import PatchRule, SkillPackage


@dataclass(frozen=True)
class TransformationPack(CapabilityPack):
    """Mechanical rewrite ability, decoupled from any Skill Package."""

    allow_patch: bool = False
    rules: tuple[PatchRule, ...] = ()
    # Scope metadata (which dependency this transformation applies to), not facts.
    package_names: tuple[str, ...] = ()
    target_version_spec: str = ""

    def patch_rules(self) -> list[PatchRule]:
        return list(self.rules)

    def allow_patch_draft(self) -> bool:
        return self.allow_patch

    @classmethod
    def from_skill(cls, skill: SkillPackage) -> TransformationPack:
        """Adapt a legacy Skill Pack into a transformation capability.

        The skill's facts stay in the RAG corpus; only its rewrite rules and
        drafting flag cross into the capability layer. A generic skill (no
        rewrite rules, drafting disabled) yields an empty, non-drafting pack.
        """
        return cls(
            id=skill.skill_id,
            name=skill.name,
            allow_patch=bool(skill.allow_patch_draft),
            rules=tuple(skill.patch_rules),
            package_names=tuple(skill.package_names),
            target_version_spec=skill.target_version_spec or "",
        )

    @classmethod
    def from_skills(cls, skills: Iterable[SkillPackage]) -> list[TransformationPack]:
        return [cls.from_skill(s) for s in skills]
