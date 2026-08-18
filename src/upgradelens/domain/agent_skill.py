"""Domain model for the new AgentSkill (SK-1).

An :class:`AgentSkill` is *not* dependency knowledge -- it is a behaviour
specification: when to apply a method, the ordered steps, the hard constraints
and the completion criteria an agent must satisfy. It replaces the
behaviour/prompt half of the legacy dependency-upgrade ``SkillPackage`` (the
facts go to the shared RAG corpus, the mechanical rewrites go to a
:class:`~upgradelens.capabilities.transformation.TransformationPack`).

Per project convention the canonical text lives in an English ``SKILL.md``; a
Chinese ``cn.md`` (or any ``<lang>.md``) is a *localized variant* registered in
``localized_variants``. The runtime loads only ``SKILL.md`` by default.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentSkill(BaseModel):
    """A capability-agnostic behaviour specification for a professional agent."""

    skill_id: str
    name: str = ""
    # capability kinds this behaviour applies to (e.g. "dependency_upgrade")
    applies_to: list[str] = Field(default_factory=list)
    # primary language of SKILL.md ("en" / "zh" / ...)
    language: str = "en"
    # localized variant language codes present on disk (e.g. ["cn"])
    localized_variants: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    description: str = ""
    when_to_use: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    # full markdown body of SKILL.md (for rendering / prompting)
    body: str = ""
    # on-disk directory the skill was loaded from
    source_path: str = ""

    def matches_kind(self, kind: str) -> bool:
        return kind in self.applies_to

    def to_instructions(self) -> str:
        """A compact instruction block for prompt injection (progressive disclosure).

        Level-1 disclosure: identity + method steps + hard constraints + completion
        criteria -- enough for an agent to *behave* correctly without shipping the
        full ``body``. The full markdown stays on disk and is loaded only when a
        human (or the UI) asks for it.
        """
        lines: list[str] = [f"# Skill: {self.name or self.skill_id} (v{self.version})"]
        if self.when_to_use:
            lines.append("When to use: " + "; ".join(self.when_to_use))
        if self.steps:
            lines.append("Steps:")
            lines.extend(f"{i}. {s}" for i, s in enumerate(self.steps, 1))
        if self.constraints:
            lines.append("Hard constraints:")
            lines.extend(f"- {c}" for c in self.constraints)
        if self.completion_criteria:
            lines.append("Complete when: " + "; ".join(self.completion_criteria))
        return "\n".join(lines)


__all__ = ["AgentSkill"]
