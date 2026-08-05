"""Unit tests for the stage 3 Skill registry and version-aware selection.

Covers the plan's acceptance criteria: version-range matching, conflict
priority, package-name alias matching, source-version narrowing, and the
generic fallback when nothing matches.
"""

from __future__ import annotations

import pytest

from upgradelens.domain.skill import GENERIC_CAPABILITY_NOTE, SkillPackage
from upgradelens.skills import SkillParseError, SkillRegistry


def _skill(
    skill_id: str,
    package_names: list[str],
    target_spec: str | None,
    *,
    priority: int = 0,
    source_spec: str | None = None,
    generic: bool = False,
    content_hash: str = "h",
) -> SkillPackage:
    skill = SkillPackage.model_validate(
        {
            "skill_id": skill_id,
            "package_names": package_names,
            "target_version_spec": target_spec,
            "source_version_spec": source_spec,
            "priority": priority,
            "support_status": "generic" if generic else "dedicated",
        }
    )
    skill.content_hash = content_hash
    return skill


def _registry() -> SkillRegistry:
    pyd_high = _skill(
        "pydantic_v1_to_v2", ["pydantic"], ">=2,<3", priority=100, source_spec=">=1,<2"
    )
    pyd_low = _skill("pydantic_experimental", ["pydantic"], ">=2,<3", priority=10)
    generic = _skill("generic", ["*"], None, generic=True, priority=0)
    return SkillRegistry([pyd_high, pyd_low, generic])


def test_version_range_match_selects_dedicated() -> None:
    sel = _registry().select_skill("pydantic", "2.0.0")
    assert sel.skill_id == "pydantic_v1_to_v2"
    assert sel.is_generic is False
    assert sel.matched_by == "version_range"
    assert sel.skill_version == "1.0.0"


def test_no_match_falls_back_to_generic() -> None:
    sel = _registry().select_skill("requests", "2.31.0")
    assert sel.is_generic is True
    assert sel.matched_by == "generic_fallback"
    assert sel.capability_note == GENERIC_CAPABILITY_NOTE


def test_higher_priority_wins_on_conflict() -> None:
    sel = _registry().select_skill("pydantic", "2.5.0")
    assert sel.skill_id == "pydantic_v1_to_v2"  # priority 100 beats 10


def test_package_name_alias_is_canonicalized() -> None:
    sel = _registry().select_skill("PyDantic", "2.0.0")
    assert sel.skill_id == "pydantic_v1_to_v2"


def test_source_version_narrows_candidates() -> None:
    # Single dedicated skill with an explicit source range; a skill without a
    # source spec always matches any source version, so we must pin it here.
    dedicated = _skill(
        "pydantic_v1_to_v2", ["pydantic"], ">=2,<3", priority=100, source_spec=">=1,<2"
    )
    generic = _skill("generic", ["*"], None, generic=True, priority=0)
    reg = SkillRegistry([dedicated, generic])

    # source 1.5 satisfies ">=1,<2" -> dedicated wins
    ok = reg.select_skill("pydantic", "2.0.0", source_version="1.5.0")
    assert ok.skill_id == "pydantic_v1_to_v2"
    # source 2.0 does NOT satisfy ">=1,<2" -> no dedicated candidate -> generic
    fallback = reg.select_skill("pydantic", "2.0.0", source_version="2.0.0")
    assert fallback.is_generic is True


def test_invalid_target_version_is_rejected() -> None:

    with pytest.raises(SkillParseError):
        _registry().select_skill("pydantic", "not-a-version")


def test_catalog_sorts_by_priority() -> None:
    catalog = _registry().catalog()
    assert [e.skill_id for e in catalog.skills][0] == "pydantic_v1_to_v2"
    assert catalog.generic_skill_id == "generic"
