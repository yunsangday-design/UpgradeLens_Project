"""Tests for AgentSkill (SK-1), RAG corpus (LS-1) and TransformationPack (LS-2)."""

from __future__ import annotations

from upgradelens.agent_skills.loader import load_builtin_agent_skills
from upgradelens.agent_skills.resolver import default_agent_skill_registry, resolve_agent_skill
from upgradelens.capabilities.transformations.loader import (
    TransformationLoadError,
    load_transformation_pack,
)
from upgradelens.corpus.loader import load_builtin_corpus


def test_builtin_agent_skills_load_with_localized_variants() -> None:
    skills = {s.skill_id for s in load_builtin_agent_skills()}
    assert {
        "evidence-grounded-review",
        "systematic-issue-diagnosis",
        "verification-before-completion",
        "safe-dependency-migration",
    } <= skills
    # every built-in skill ships a Chinese localized variant
    for s in load_builtin_agent_skills():
        assert "cn" in s.localized_variants
        assert s.language == "en"


def test_agent_skill_resolves_by_kind_and_locale() -> None:
    reg = default_agent_skill_registry()
    # dependency_upgrade has a dedicated method skill; cross-cutting skills also
    # apply, but the resolver prefers the most specific one.
    dep_en = reg.resolve("dependency_upgrade", locale="en")
    assert dep_en is not None
    assert dep_en.skill_id == "safe-dependency-migration"
    # zh locale still resolves to the (variant-bearing) method skill
    dep_zh = resolve_agent_skill("dependency_upgrade", locale="zh-CN")
    assert dep_zh is not None
    assert dep_zh.skill_id == "safe-dependency-migration"
    assert "cn" in dep_zh.localized_variants


def test_agent_skill_has_behaviour_not_facts() -> None:
    # The dedicated method skill is a *behaviour* spec, not a knowledge pack:
    # it declares (structurally) that version facts belong to the corpus.
    s = resolve_agent_skill("dependency_upgrade")
    assert s is not None
    assert s.skill_id == "safe-dependency-migration"
    assert s.applies_to == ["dependency_upgrade"]
    assert s.steps
    assert s.constraints
    assert s.evidence_policy.get("facts_belong_to_corpus") is True
    # It must not claim to own hard-coded version facts / removed APIs.
    assert "rag corpus" in (s.body + s.description).lower()


def test_agent_skill_to_instructions_is_compact_disclosure() -> None:
    s = resolve_agent_skill("dependency_upgrade")
    assert s is not None
    block = s.to_instructions()
    assert block.startswith("# Skill:")
    assert "Steps:" in block and "Hard constraints:" in block
    # level-1 disclosure never ships the full markdown body
    assert len(block) < len(s.body) or not s.body


def test_capability_agent_carries_skill_digest_in_notes() -> None:
    """SK-1-3: the runner path resolves the AgentSkill and echoes it into notes."""
    from upgradelens.agent.runtime import (
        AgentIdentity,
        AgentKind,
        AgentRunContext,
        TaskEnvelope,
        new_run_id,
    )
    from upgradelens.agent.spec import default_registry

    ctx = AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.DEPENDENCY_UPGRADE),
        mode="fake",
        locale="zh-CN",
    )
    task = TaskEnvelope(kind="dependency_upgrade", goal="demo goal")
    spec = default_registry().resolve(AgentKind.DEPENDENCY_UPGRADE)
    assert spec.run is not None
    result = spec.run(ctx, task)
    digest = result.notes.get("agent_skill")
    assert digest is not None
    assert digest["skill_id"] == "safe-dependency-migration"
    assert digest["steps"] and digest["constraints"]
    # progressive disclosure: the full instruction block is NOT dumped into notes
    assert "instructions" not in digest


def test_corpus_carries_legacy_skill_facts() -> None:
    specs = load_builtin_corpus()
    packages = {sp.canonical_package for sp in specs}
    assert "pydantic" in packages
    assert "sqlalchemy" in packages
    pyd = [sp for sp in specs if sp.canonical_package == "pydantic"]
    assert any(sp.source_type == "migration_guide" for sp in pyd)


def test_transformation_pack_migrated_from_skill() -> None:
    pack = load_transformation_pack("pydantic_v1_to_v2")
    assert pack.allow_patch is True
    assert pack.target_version_spec == ">=2,<3"
    assert len(pack.rules) == 2
    ids = {r.id for r in pack.rules}
    assert "pydantic_validator_to_field_validator" in ids
    assert "pydantic_dict_to_model_dump" in ids


def test_transformation_pack_missing_raises() -> None:
    try:
        load_transformation_pack("does_not_exist")
        raise AssertionError("expected TransformationLoadError")
    except TransformationLoadError:
        pass
