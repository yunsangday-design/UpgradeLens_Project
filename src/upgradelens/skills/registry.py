"""Skill registry and version-aware selection (plan sections 8.9, 3).

The registry holds every loaded :class:`SkillPackage` and, given a dependency
name and target version, picks the highest-priority dedicated skill whose
package names and version range match. When nothing matches, it falls back to
the registered *generic* skill, which carries a lowered capability statement.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from upgradelens.domain.skill import (
    SkillCatalog,
    SkillPackage,
    SkillSelection,
    catalog_entry_from_skill,
    selection_from_skill,
)
from upgradelens.skills.loader import SkillParseError, discover_skills


class SkillRegistry:
    """In-memory collection of skills with version-aware resolution."""

    def __init__(
        self,
        skills: Iterable[SkillPackage],
        *,
        generic_skill_id: str | None = None,
    ) -> None:
        self._skills: dict[str, SkillPackage] = {}
        self._generic_skill_id: str | None = generic_skill_id
        for skill in skills:
            self.add(skill)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_directory(
        cls, base_dir: str | Path, *, generic_skill_id: str | None = None
    ) -> SkillRegistry:
        return cls(discover_skills(base_dir), generic_skill_id=generic_skill_id)

    def add(self, skill: SkillPackage) -> None:
        self._skills[skill.skill_id] = skill
        if self._generic_skill_id is None and skill.is_generic:
            self._generic_skill_id = skill.skill_id

    # -- access ----------------------------------------------------------

    def get(self, skill_id: str) -> SkillPackage | None:
        return self._skills.get(skill_id)

    def all(self) -> list[SkillPackage]:
        return list(self._skills.values())

    def catalog(self) -> SkillCatalog:
        entries = [catalog_entry_from_skill(s) for s in self._skills.values()]
        entries.sort(key=lambda e: (-e.priority, e.skill_id))
        return SkillCatalog(skills=entries, generic_skill_id=self._generic_skill_id)

    # -- selection -------------------------------------------------------

    def select_skill(
        self,
        dependency_name: str,
        target_version: str,
        source_version: str | None = None,
    ) -> SkillSelection:
        canonical = canonicalize_name(dependency_name)
        try:
            target = Version(str(target_version))
        except InvalidVersion as exc:
            raise SkillParseError(f"invalid target version {target_version!r}: {exc}") from exc

        candidates: list[SkillPackage] = []
        for skill in self._skills.values():
            if canonical not in skill.canonical_package_names:
                continue
            if not self._version_in(skill.target_version_spec, target):
                continue
            if source_version is not None and skill.source_version_spec:
                if not self._version_in(skill.source_version_spec, Version(str(source_version))):
                    continue
            candidates.append(skill)

        if not candidates:
            return self._generic_selection(dependency_name)

        best = max(candidates, key=lambda s: s.priority)
        return selection_from_skill(best, dependency_name, matched_by="version_range")

    def _generic_selection(self, dependency_name: str) -> SkillSelection:
        if self._generic_skill_id is None:
            raise SkillParseError(
                "no skill matched the request and no generic fallback is registered"
            )
        generic = self._skills[self._generic_skill_id]
        return selection_from_skill(generic, dependency_name, matched_by="generic_fallback")

    @staticmethod
    def _version_in(spec: str | None, version: Version) -> bool:
        if spec is None:
            return True
        try:
            return version in SpecifierSet(spec)
        except InvalidSpecifier:  # pragma: no cover - validated at load time
            return False
