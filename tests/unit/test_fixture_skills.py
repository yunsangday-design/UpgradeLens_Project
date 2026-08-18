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
    assert "sqlalchemy_v1_to_v2" in ids


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
    # migration (MA/LS): dedicated dependency-upgrade pack is superseded by the
    # shared RAG corpus + TransformationPack + AgentSkill path.
    assert pyd.deprecated is True


def test_adding_a_skill_needs_no_core_change() -> None:
    # The registry is built purely from discovered directories, so the number
    # of skills is not hard-coded into the workflow (plan section 3, line 1709).
    assert len(discover_builtin_skills()) >= 3


def test_sqlalchemy_skill_has_expected_shape() -> None:
    sqla = builtin_registry().get("sqlalchemy_v1_to_v2")
    assert sqla is not None
    assert sqla.support_status == "dedicated"
    assert sqla.priority == 100
    assert sqla.allow_patch_draft is True
    # 2 import relocations (auto-safe) + 3 semantic rewrites (review-only).
    assert len(sqla.patterns) == 5
    assert len(sqla.sources) == 3
    # Only the two import relocations have safe mechanical rules.
    assert len(sqla.patch_rules) == 2
    for rule in sqla.patch_rules:
        assert rule.target_regex
        assert rule.replacement
    assert sqla.content_hash  # hashed from the on-disk YAML files


def test_resolve_sqlalchemy_uses_dedicated_pack() -> None:
    sel = builtin_registry().select_skill("sqlalchemy", "2.0.0")
    assert sel.skill_id == "sqlalchemy_v1_to_v2"
    assert sel.is_generic is False
    assert sel.matched_by == "version_range"
    assert sel.skill_version == "1.0.0"
    assert sel.package_name == "sqlalchemy"
    # deprecated but still selectable until UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION is set
    assert sel.deprecated is True


def test_deprecated_skill_skipped_when_selection_disabled(monkeypatch) -> None:
    # With the legacy-skill disable flag on, deprecated dedicated packs are
    # skipped and selection falls back to the generic capability note.
    monkeypatch.setenv("UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION", "1")
    sel = builtin_registry().select_skill("pydantic", "2.0.0")
    assert sel.is_generic is True
    assert sel.matched_by == "generic_fallback"
    assert sel.deprecated is False
    assert GENERIC_CAPABILITY_NOTE in sel.capability_note


def test_resolve_sqlalchemy_14_falls_back_to_generic() -> None:
    # select_skill matches against the *target* range (>=2,<3); a 1.4 source
    # version correctly falls back to the generic pack rather than the 2.0 pack.
    sel = builtin_registry().select_skill("sqlalchemy", "1.4.50")
    assert sel.is_generic is True
    assert sel.matched_by == "generic_fallback"


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
    # deprecated but still selectable until UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION is set
    assert sel.deprecated is True
