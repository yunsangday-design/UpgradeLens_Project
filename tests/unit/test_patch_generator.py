"""Offline tests for the stage 8 patch-draft generator."""

from __future__ import annotations

from pathlib import Path

from upgradelens.models.impact import EvidenceBundle, EvidenceItem
from upgradelens.patch import generate_patch_draft
from upgradelens.skills import SkillRegistry, builtin_registry
from upgradelens.verify.models import EvidenceStatus, VerifiedRisk


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "models.py").write_text(
        "from pydantic import BaseModel, validator\n"
        "\n"
        "class User(BaseModel):\n"
        "    name: str\n"
        "\n"
        "    @validator('name')\n"
        "    def check_name(cls, v):\n"
        "        return v\n"
        "\n"
        "    def dump(self):\n"
        "        return self.dict()\n",
        encoding="utf-8",
    )
    # A file that uses .dict() but is NOT referenced by any evidence -> must be
    # left untouched by the generator (safety: only verified locations).
    (repo / "src" / "other.py").write_text("def f(o):\n    return o.dict()\n", encoding="utf-8")
    return repo


def _bundle() -> EvidenceBundle:
    bundle = EvidenceBundle()
    bundle.add(
        EvidenceItem(
            evidence_id="e1",
            kind="code_usage",
            summary="validator usage",
            detail="",
            meta={
                "path": "src/models.py",
                "line": 6,
                "usage_kind": "decorator",
                "symbol": "validator",
                "content_hash": "x",
                "is_test_code": False,
            },
        )
    )
    bundle.add(
        EvidenceItem(
            evidence_id="e2",
            kind="code_usage",
            summary="dict usage",
            detail="",
            meta={
                "path": "src/models.py",
                "line": 11,
                "usage_kind": "method_call",
                "symbol": "dict",
                "content_hash": "x",
                "is_test_code": False,
            },
        )
    )
    return bundle


def _risk() -> VerifiedRisk:
    return VerifiedRisk(
        risk_id="r1",
        title="pydantic v1 usage",
        status=EvidenceStatus.VERIFIED,
        severity="medium",
        model_severity="medium",
        code_evidence_ids=["e1", "e2"],
    )


def test_patch_draft_applies_low_risk_only_without_quality_model(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    skill = builtin_registry().get("pydantic_v1_to_v2")
    draft = generate_patch_draft(repo, [_risk()], skill, _bundle())
    assert not draft.is_empty
    diff = draft.to_unified_diff()
    # The low-risk .dict() -> .model_dump() rewrite is applied automatically.
    assert ".model_dump(" in diff
    assert "@field_validator(" not in diff  # needs quality model
    assert "src/other.py" not in diff  # safety: unreferenced file untouched
    # The working tree is never mutated.
    assert "self.dict()" in (repo / "src" / "models.py").read_text(encoding="utf-8")


def test_patch_draft_applies_quality_model_rule_when_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    skill = builtin_registry().get("pydantic_v1_to_v2")
    draft = generate_patch_draft(repo, [_risk()], skill, _bundle(), quality_model_available=True)
    diff = draft.to_unified_diff()
    assert "@field_validator('name')" in diff
    assert ".model_dump(" in diff
    assert "pydantic_dict_to_model_dump" in draft.applied_rules
    assert "pydantic_validator_to_field_validator" in draft.applied_rules


def test_patch_draft_skips_when_disallowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry: SkillRegistry = builtin_registry()
    skill = registry.get("pydantic_v1_to_v2")
    skill.allow_patch_draft = False
    draft = generate_patch_draft(repo, [_risk()], skill, _bundle())
    assert draft.is_empty
    assert "does not permit" in draft.notes
    assert draft.to_unified_diff() == ""


def test_patch_draft_ignores_unverified_risks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    skill = builtin_registry().get("pydantic_v1_to_v2")
    risk = VerifiedRisk(
        risk_id="r1",
        title="pydantic v1 usage",
        status=EvidenceStatus.PARTIALLY_VERIFIED,
        severity="medium",
        model_severity="medium",
        code_evidence_ids=["e1", "e2"],
    )
    draft = generate_patch_draft(repo, [risk], skill, _bundle())
    assert draft.is_empty


def test_patch_draft_emits_valid_unified_diff_shape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    skill = builtin_registry().get("pydantic_v1_to_v2")
    draft = generate_patch_draft(repo, [_risk()], skill, _bundle())
    text = draft.to_unified_diff()
    assert text.startswith("--- a/src/models.py\n")
    assert "@@ -" in text
    # The original line is shown removed and the rewrite added.
    assert "-        return self.dict()" in text
    assert "+        return self.model_dump()" in text
