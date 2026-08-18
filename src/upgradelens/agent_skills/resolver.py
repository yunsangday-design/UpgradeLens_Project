"""Resolve the right AgentSkill for a capability kind + locale (SK-1-3)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from upgradelens.agent_skills.loader import load_builtin_agent_skills
from upgradelens.domain.agent_skill import AgentSkill


class AgentSkillRegistry:
    """Index AgentSkills by capability kind, with locale-aware resolution."""

    def __init__(self, skills: Iterable[AgentSkill]) -> None:
        self._by_id: dict[str, AgentSkill] = {s.skill_id: s for s in skills}
        self._by_kind: dict[str, list[AgentSkill]] = defaultdict(list)
        for s in skills:
            for kind in s.applies_to:
                self._by_kind[kind].append(s)

    def register(self, skill: AgentSkill) -> None:
        self._by_id[skill.skill_id] = skill
        for kind in skill.applies_to:
            self._by_kind[kind].append(skill)

    def get(self, skill_id: str) -> AgentSkill | None:
        return self._by_id.get(skill_id)

    def for_kind(self, kind: str) -> list[AgentSkill]:
        return list(self._by_kind.get(kind, []))

    def resolve(self, kind: str, *, locale: str = "en") -> AgentSkill | None:
        """Pick the skill for ``kind``, preferring the most specific + localized one.

        When several skills apply to a kind (e.g. a cross-cutting behaviour like
        *evidence-grounded-review* and a method like *safe-dependency-migration*),
        the resolver prefers the **most specific** skill -- one whose
        ``applies_to`` is exactly ``[kind]`` -- over a broad, multi-capability one.
        Locale ``zh-CN`` / ``zh`` then prefers a skill that ships a ``cn`` variant.
        """
        candidates = self.for_kind(kind)
        if not candidates:
            return None
        lang = locale.split("-")[0].lower()  # "zh-CN" -> "zh"

        def _specificity(s: AgentSkill) -> tuple[int, int]:
            exact = 0 if s.applies_to == [kind] else 1
            return (exact, len(s.applies_to))

        ordered = sorted(candidates, key=_specificity)
        if lang != "en":
            for s in ordered:
                if lang in s.localized_variants:
                    return s
        for s in ordered:
            if s.language == "en" or s.localized_variants:
                return s
        return ordered[0]


def default_agent_skill_registry() -> AgentSkillRegistry:
    return AgentSkillRegistry(load_builtin_agent_skills())


def resolve_agent_skill(kind: str, *, locale: str = "en") -> AgentSkill | None:
    """Convenience: resolve a built-in AgentSkill for ``kind``/``locale``."""
    return default_agent_skill_registry().resolve(kind, locale=locale)


__all__ = [
    "AgentSkillRegistry",
    "default_agent_skill_registry",
    "resolve_agent_skill",
]
