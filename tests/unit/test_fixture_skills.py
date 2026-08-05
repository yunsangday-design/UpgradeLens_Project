"""Contract test for the built-in Skill Packs (plan section 3).

Mirrors the stage 1/2 fixture contract idea: the bundled skills under
``src/upgradelens/skills/builtin/`` are the single source of truth for what a
valid, shippable Skill Pack looks like, and this test fails loudly if a pack
is malformed or drifts from the documented shape.
"""

from __future__ import annotations

from upgradelens.domain.skill import GENERIC_CAPABILITY_NOTE
from upgradelens.skills import builtin_registry, discover_builtin_skills


def test_builtin_skills_load_without_error() -> None:
    ids = {s.skill_id for s in discover_builtin_skills()}
    assert "pydantic_v1_to_v2" in ids
    assert "generic_python_dependency" in ids


def test_pydantic_skill_has_expected_shape() -> None:
    pyd = builtin_registry().get("pydantic_v1_to_v2")
    assert pyd is not None
    assert pyd.support_status == "dedicated"
    assert pyd.priority == 100
    assert len(pyd.patterns) == 7  # plan section 9.7 first batch
    assert len(pyd.sources) == 2
    assert len(pyd.patch_rules) == 2
    assert pyd.allow_patch_draft is True
    assert pyd.content_hash  # hashed from the on-disk YAML files


def test_adding_a_skill_needs_no_core_change() -> None:
    # The registry is built purely from discovered directories, so the number
    # of skills is not hard-coded into the workflow (plan section 3, line 1709).
    assert len(discover_builtin_skills()) >= 2


def test_generic_skill_lowers_capability_claims() -> None:
    reg = builtin_registry()
    gen = reg.get("generic_python_dependency")
    assert gen is not None
    assert gen.support_status == "generic"
    assert gen.allow_patch_draft is False
    assert gen.patch_rules == []  # a generic pack proposes no mechanical rewrites

    sel = reg.select_skill("some-random-dependency", "2.0.0")
    assert sel.is_generic is True
    assert sel.matched_by == "generic_fallback"
    assert sel.capability_note == GENERIC_CAPABILITY_NOTE


def test_resolve_pydantic_uses_dedicated_pack() -> None:
    sel = builtin_registry().select_skill("pydantic", "2.0.0")
    assert sel.skill_id == "pydantic_v1_to_v2"
    assert sel.is_generic is False
    assert sel.matched_by == "version_range"
    assert sel.skill_version == "1.0.0"
    assert sel.package_name == "pydantic"
