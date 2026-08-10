"""Offline tests for the stage 5 / B5 capability pack split.

These pin the contract that the patch transformation ability is decoupled from
the fact-bearing Skill Pack: a :class:`TransformationPack` carries only rewrite
rules + a drafting flag, and the main flow never needs a pack to be present.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities import CapabilityPack, CapabilityRegistry, TransformationPack
from upgradelens.patch import generate_patch_draft
from upgradelens.skills import builtin_registry


def _pydantic() -> TransformationPack:
    skill = builtin_registry().get("pydantic_v1_to_v2")
    assert skill is not None
    return TransformationPack.from_skill(skill)


def _generic() -> TransformationPack:
    skill = builtin_registry().get("generic_python_dependency")
    assert skill is not None
    return TransformationPack.from_skill(skill)


def test_capability_pack_defaults_are_safe_noops():
    pack = CapabilityPack(id="x")
    assert pack.applies_to(package="any", source_version="1", target_version="2") is True
    assert pack.patch_rules() == []
    assert pack.allow_patch_draft() is False
    assert pack.parse_manifest(text="", filename="pyproject.toml") is None
    assert pack.validate(report=None, bundle=None) is None
    assert pack.recommend_tests(bundle=None) is None


def test_transformation_pack_adapts_dedicated_skill():
    pack = _pydantic()
    assert pack.id == "pydantic_v1_to_v2"
    assert pack.allow_patch_draft() is True
    rules = pack.patch_rules()
    assert rules  # has rewrite rules
    assert all(r.id for r in rules)
    assert "pydantic" in pack.package_names
    assert pack.target_version_spec


def test_transformation_pack_generic_skill_is_empty_and_nondrafting():
    pack = _generic()
    assert pack.id == "generic_python_dependency"
    assert pack.allow_patch_draft() is False
    assert pack.patch_rules() == []
    # Marked as generic so it never participates in mechanical rewrites.
    assert pack.name


def test_registry_catalog_derives_from_skills():
    reg = CapabilityRegistry.from_skills(builtin_registry().all())
    catalog = reg.catalog()
    ids = {c["id"] for c in catalog}
    assert "pydantic_v1_to_v2" in ids
    assert "generic_python_dependency" in ids

    pydantic_entry = next(c for c in catalog if c["id"] == "pydantic_v1_to_v2")
    assert pydantic_entry["allow_patch_draft"] is True
    assert pydantic_entry["type"] == "TransformationPack"

    generic_entry = next(c for c in catalog if c["id"] == "generic_python_dependency")
    assert generic_entry["allow_patch_draft"] is False


def test_missing_pack_is_safe():
    # A None capability must yield an empty, non-drafting draft without touching
    # the repo or the evidence bundle (it short-circuits before any rule match).
    draft = generate_patch_draft(Path("/nonexistent"), [], None, object())
    assert draft is not None
    assert draft.allow_patch_draft is False
    assert draft.applied_rules == []
