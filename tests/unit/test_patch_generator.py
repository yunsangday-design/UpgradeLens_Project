"""Offline tests for the stage 8 patch-draft generator."""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities import TransformationPack
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
    draft = generate_patch_draft(repo, [_risk()], TransformationPack.from_skill(skill), _bundle())
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
    draft = generate_patch_draft(
        repo,
        [_risk()],
        TransformationPack.from_skill(skill),
        _bundle(),
        quality_model_available=True,
    )
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
    draft = generate_patch_draft(repo, [_risk()], TransformationPack.from_skill(skill), _bundle())
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
    draft = generate_patch_draft(repo, [risk], TransformationPack.from_skill(skill), _bundle())
    assert draft.is_empty


def test_patch_draft_emits_valid_unified_diff_shape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    skill = builtin_registry().get("pydantic_v1_to_v2")
    draft = generate_patch_draft(repo, [_risk()], TransformationPack.from_skill(skill), _bundle())
    text = draft.to_unified_diff()
    assert text.startswith("--- a/src/models.py\n")
    assert "@@ -" in text
    # The original line is shown removed and the rewrite added.
    assert "-        return self.dict()" in text
    assert "+        return self.model_dump()" in text


def _sqla_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    # Line 1: import relocated by rule SQLA001.
    # Line 2: import relocated by rule SQLA002.
    # Line 4: a 1.x query (SQLA003) that must NOT be auto-patched.
    (repo / "src" / "db_models.py").write_text(
        "from sqlalchemy.ext.declarative import declarative_base, declared_attr\n"
        "from sqlalchemy.ext.hybrid import hybrid_property\n"
        "\n"
        "Base = declarative_base()\n",
        encoding="utf-8",
    )
    return repo


def _sqla_bundle() -> EvidenceBundle:
    bundle = EvidenceBundle()
    bundle.add(
        EvidenceItem(
            evidence_id="se1",
            kind="code_usage",
            summary="declarative import",
            detail="",
            meta={
                "path": "src/db_models.py",
                "line": 1,
                "usage_kind": "import",
                "symbol": "declarative_base",
                "content_hash": "x",
                "is_test_code": False,
            },
        )
    )
    bundle.add(
        EvidenceItem(
            evidence_id="se2",
            kind="code_usage",
            summary="hybrid import",
            detail="",
            meta={
                "path": "src/db_models.py",
                "line": 2,
                "usage_kind": "import",
                "symbol": "hybrid_property",
                "content_hash": "x",
                "is_test_code": False,
            },
        )
    )
    return bundle


def _sqla_risk() -> VerifiedRisk:
    return VerifiedRisk(
        risk_id="sr1",
        title="sqlalchemy 1.x imports",
        status=EvidenceStatus.VERIFIED,
        severity="low",
        model_severity="low",
        code_evidence_ids=["se1", "se2"],
    )


def test_sqlalchemy_patch_relocates_imports(tmp_path: Path) -> None:
    repo = _sqla_repo(tmp_path)
    skill = builtin_registry().get("sqlalchemy_v1_to_v2")
    draft = generate_patch_draft(
        repo, [_sqla_risk()], TransformationPack.from_skill(skill), _sqla_bundle()
    )
    diff = draft.to_unified_diff()
    assert not draft.is_empty
    # Both import relocations are applied (auto-safe, no quality model needed).
    assert "from sqlalchemy.orm import declarative_base, declared_attr" in diff
    assert "from sqlalchemy.orm import hybrid_property" in diff
    assert "sqlalchemy_ext_declarative_to_orm" in draft.applied_rules
    assert "sqlalchemy_ext_hybrid_to_orm" in draft.applied_rules
    # The working tree is never mutated.
    src = (repo / "src" / "db_models.py").read_text(encoding="utf-8")
    assert "from sqlalchemy.ext.declarative import" in src


def test_sqlalchemy_query_rewrite_not_autopatched(tmp_path: Path) -> None:
    # session.query() (SQLA003) is flagged as a risk but has NO mechanical
    # rule, so even with the quality model enabled the generator must emit
    # nothing -- it requires human review.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "queries.py").write_text(
        "def load(session):\n    return session.query(User).filter(User.id == 1).all()\n",
        encoding="utf-8",
    )
    bundle = EvidenceBundle()
    bundle.add(
        EvidenceItem(
            evidence_id="qe1",
            kind="code_usage",
            summary="session.query",
            detail="",
            meta={
                "path": "src/queries.py",
                "line": 2,
                "usage_kind": "method_call",
                "symbol": "query",
                "content_hash": "x",
                "is_test_code": False,
            },
        )
    )
    risk = VerifiedRisk(
        risk_id="qr1",
        title="session.query usage",
        status=EvidenceStatus.VERIFIED,
        severity="high",
        model_severity="high",
        code_evidence_ids=["qe1"],
    )
    skill = builtin_registry().get("sqlalchemy_v1_to_v2")
    draft = generate_patch_draft(
        repo, [risk], TransformationPack.from_skill(skill), bundle, quality_model_available=True
    )
    assert draft.is_empty
    assert draft.to_unified_diff() == ""


def test_sqlalchemy_patch_skips_when_disallowed(tmp_path: Path) -> None:
    repo = _sqla_repo(tmp_path)
    registry: SkillRegistry = builtin_registry()
    skill = registry.get("sqlalchemy_v1_to_v2")
    skill.allow_patch_draft = False
    draft = generate_patch_draft(
        repo, [_sqla_risk()], TransformationPack.from_skill(skill), _sqla_bundle()
    )
    assert draft.is_empty
    assert "does not permit" in draft.notes
