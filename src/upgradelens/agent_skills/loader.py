"""Load AgentSkills from disk (SK-1-2).

Each AgentSkill is a directory containing a canonical English ``SKILL.md`` and,
optionally, localized variants such as ``cn.md``. The loader parses the YAML
front-matter of ``SKILL.md`` into an :class:`AgentSkill` and registers any
``<lang>.md`` files it finds as localized variants.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from upgradelens.domain.agent_skill import AgentSkill

# Canonical English skill file; localized variants follow the ``<lang>.md`` pattern.
SKILL_MD = "SKILL.md"


class AgentSkillParseError(ValueError):
    """Raised when an AgentSkill directory cannot be parsed."""


def _read_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        # A skill may omit front-matter and rely on the body alone.
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        raise AgentSkillParseError("unterminated YAML front-matter")
    raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        loaded: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AgentSkillParseError(f"invalid skill front-matter: {exc}") from exc
    meta: dict[str, Any] = loaded or {}
    if not isinstance(meta, dict):
        raise AgentSkillParseError("skill front-matter must be a mapping")
    return meta, body


def load_agent_skill(skill_dir: str | Path) -> AgentSkill:
    """Load one AgentSkill from its directory."""
    base = Path(skill_dir)
    skill_md = base / SKILL_MD
    if not skill_md.is_file():
        raise AgentSkillParseError(f"missing {SKILL_MD} in {base}")
    text = skill_md.read_text(encoding="utf-8")
    meta, body = _read_frontmatter(text)

    variants: list[str] = []
    for md in base.glob("*.md"):
        if md.name == SKILL_MD:
            continue
        lang = md.stem
        variants.append(lang)

    return AgentSkill(
        skill_id=str(meta.get("skill_id", base.name)),
        name=str(meta.get("name", base.name)),
        applies_to=[str(x) for x in meta.get("applies_to", [])],
        language=str(meta.get("language", "en")),
        localized_variants=sorted(variants),
        version=str(meta.get("version", "1.0.0")),
        description=str(meta.get("description", "")),
        when_to_use=[str(x) for x in meta.get("when_to_use", [])],
        steps=[str(x) for x in meta.get("steps", [])],
        constraints=[str(x) for x in meta.get("constraints", [])],
        completion_criteria=[str(x) for x in meta.get("completion_criteria", [])],
        evidence_policy=dict(meta.get("evidence_policy", {})),
        body=body,
        source_path=str(base),
    )


def discover_agent_skills(base_dir: str | Path) -> list[AgentSkill]:
    """Load every AgentSkill directory directly under ``base_dir``."""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    skills: list[AgentSkill] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / SKILL_MD).is_file():
            skills.append(load_agent_skill(child))
    return skills


def default_agent_skill_dir() -> Path:
    """Built-in AgentSkill directory shipped with the package."""
    return Path(__file__).resolve().parent / "builtin"


def load_builtin_agent_skills() -> list[AgentSkill]:
    return discover_agent_skills(default_agent_skill_dir())


__all__ = [
    "AgentSkillParseError",
    "load_agent_skill",
    "discover_agent_skills",
    "default_agent_skill_dir",
    "load_builtin_agent_skills",
]
