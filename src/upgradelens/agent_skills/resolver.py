"""Resolve the right AgentSkill for a capability kind + locale (SK-1-3)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from upgradelens.agent_skills.loader import load_builtin_agent_skills
from upgradelens.domain.agent_skill import AgentSkill

# The explainable routing contract (implementation-plan SK-1-3 acceptance):
# which behaviour skill each capability kind resolves to by default. It exists
# so per-kind routing is an explicit, auditable decision instead of an emergent
# property of `len(applies_to)` tiebreaks.
_ROUTING_CONTRACT: dict[str, str] = {
    "dependency_upgrade": "safe-dependency-migration",
    "pr_review": "evidence-grounded-review",
    "security_review": "evidence-grounded-review",
    "issue_repair": "systematic-issue-diagnosis",
    "breaking_change": "evidence-grounded-review",
}


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
        """Pick the skill for ``kind`` with an explainable routing contract.

        The routing table below is the contract from the implementation plan's
        SK-1-3 acceptance criteria -- it wins over generic specificity so the
        behaviour is deterministic and auditable:

        * ``dependency_upgrade`` -> safe-dependency-migration (exact, method)
        * ``pr_review`` / ``security_review`` / ``breaking_change``
          -> evidence-grounded-review (review kinds)
        * ``issue_repair`` -> systematic-issue-diagnosis (diagnosis method)

        Kinds outside the table fall back to "most specific first" (fewest
        ``applies_to``); an unknown kind resolves to ``None``. Locale
        ``zh-CN`` / ``zh`` then prefers a skill that ships a ``cn`` variant.
        """
        candidates = self.for_kind(kind)
        if not candidates:
            return None
        lang = locale.split("-")[0].lower()  # "zh-CN" -> "zh"

        ordered = self._order_candidates(kind, candidates)
        if lang != "en":
            for s in ordered:
                if lang in s.localized_variants:
                    return s
        for s in ordered:
            if s.language == "en" or s.localized_variants:
                return s
        return ordered[0]

    def _order_candidates(self, kind: str, candidates: list[AgentSkill]) -> list[AgentSkill]:
        preferred = _ROUTING_CONTRACT.get(kind)
        if preferred is not None and any(s.skill_id == preferred for s in candidates):
            return sorted(
                candidates,
                key=lambda s: (0 if s.skill_id == preferred else 1, len(s.applies_to)),
            )
        return sorted(candidates, key=lambda s: len(s.applies_to))


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
