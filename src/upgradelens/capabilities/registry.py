"""Aggregate optional capability packs and surface the catalog."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from upgradelens.capabilities.base import CapabilityPack
from upgradelens.domain.skill import SkillPackage


@dataclass
class CapabilityRegistry:
    """Holds the optional capability packs available to an assessment."""

    packs: list[CapabilityPack] = field(default_factory=list)

    def register(self, pack: CapabilityPack) -> None:
        self.packs.append(pack)

    def all(self) -> list[CapabilityPack]:
        return list(self.packs)

    def catalog(self) -> list[dict[str, object]]:
        """A JSON-serializable description of every registered pack."""
        return [
            {
                "id": p.id,
                "name": p.name,
                "type": type(p).__name__,
                "allow_patch_draft": p.allow_patch_draft(),
            }
            for p in self.packs
        ]

    @classmethod
    def from_skills(cls, skills: Iterable[SkillPackage]) -> CapabilityRegistry:
        """Compatibility shim: derive the capability catalog from legacy skill packs.

        During the transition, the skill corpus is the source of truth for which
        transformations exist; each skill becomes a :class:`TransformationPack`. A
        generic skill (no rewrite rules, drafting disabled) still appears, marking
        the boundary where the old skill list used to be the only "capability"
        surface.
        """
        from upgradelens.capabilities.transformation import TransformationPack

        reg = cls()
        for skill in skills:
            reg.register(TransformationPack.from_skill(skill))
        return reg
