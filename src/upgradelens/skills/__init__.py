"""Skill Pack infrastructure (plan sections 3, 8.9-8.12).

Public surface for loading and selecting Skill Packs. Built-in packs ship next
to the package under ``skills/builtin/`` so they are discoverable without any
configuration change (plan section 3: "新增 Skill 不需要修改主工作流").
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.domain.skill import (
    DocSource,
    PatchRule,
    SkillCatalog,
    SkillCatalogEntry,
    SkillPackage,
    SkillSelection,
    UsagePattern,
)
from upgradelens.skills.loader import (
    PATCH_RULES_FILE,
    PATTERNS_FILE,
    SKILL_FILE,
    SOURCES_FILE,
    SkillParseError,
    discover_skills,
    load_skill_package,
)
from upgradelens.skills.registry import SkillRegistry

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin"


def discover_builtin_skills() -> list[SkillPackage]:
    """Load every built-in Skill Pack shipped with UpgradeLens."""
    return discover_skills(BUILTIN_SKILLS_DIR)


def builtin_registry() -> SkillRegistry:
    """A registry pre-populated with the built-in Skill Packs."""
    return SkillRegistry(discover_builtin_skills())


__all__ = [
    "BUILTIN_SKILLS_DIR",
    "DocSource",
    "PATCH_RULES_FILE",
    "PATTERNS_FILE",
    "PatchRule",
    "SKILL_FILE",
    "SOURCES_FILE",
    "SkillCatalog",
    "SkillCatalogEntry",
    "SkillPackage",
    "SkillParseError",
    "SkillRegistry",
    "SkillSelection",
    "UsagePattern",
    "builtin_registry",
    "discover_builtin_skills",
    "discover_skills",
    "load_skill_package",
]
