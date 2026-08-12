"""Unit tests for the stage 6 Evidence Verifier (plan section 13.4).

Every test builds a *real* miniature repository and runs the real scanner, so
the evidence under test is genuine rather than hand-written. Only the model
report is synthesised, because that is exactly the untrusted input the verifier
exists to police.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.analyzers import scan_code_evidence
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.models.impact import (
    EvidenceBundle,
    EvidenceItem,
    ImpactReport,
    RiskItem,
    build_bundle,
)
from upgradelens.skills import builtin_registry
from upgradelens.verify import EvidenceStatus, IssueCode, verify_report
from upgradelens.verify.models import Conclusion

PRODUCTION = """\
from pydantic import BaseModel, validator


class User(BaseModel):
    name: str

    @validator("name")
    def check(cls, v):
        return v
"""

TEST_FILE = """\
from src.models import User


def test_user():
    assert User(name="a").name == "a"
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small but realistic repository using pydantic v1 APIs."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "models.py").write_text(PRODUCTION, encoding="utf-8")
    (tmp_path / "tests" / "test_models.py").write_text(TEST_FILE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def scanned(repo: Path) -> CodeEvidenceReport:
    return scan_code_evidence(repo, "pydantic")


@pytest.fixture
def skill():
    found = builtin_registry().get("pydantic_v1_to_v2")
    assert found is not None
    return found


def _doc_item(skill, evidence_id: str = "doc:test:1") -> EvidenceItem:
    """An official documentation chunk tied to the skill's first source."""
    return EvidenceItem(
        evidence_id=evidence_id,
        kind="doc_chunk",
        summary="validator was replaced by field_validator",
        detail="In Pydantic v2 `validator` is deprecated in favour of `field_validator`.",
        meta={"source_id": skill.sources[0].id, "chunk_id": "c1"},
    )


def _bundle_with_doc(scanned: CodeEvidenceReport, skill) -> EvidenceBundle:
    bundle = build_bundle(scanned, [], dependency="pydantic")
    bundle.add(_doc_item(skill))
    return bundle


def _report(risks: list[RiskItem], **kw) -> ImpactReport:
    payload = {
        "target_dependency": "pydantic",
        "source_version_spec": "1.10",
        "target_version_spec": "2.7",
        "risks": risks,
    }
    payload.update(kw)
    return ImpactReport(**payload)


def _first_code_id(bundle: EvidenceBundle, symbol: str) -> str:
    for item in bundle.by_kind("code_usage"):
        if item.meta.get("symbol") == symbol:
            return item.evidence_id
    raise AssertionError(f"no code evidence for symbol {symbol!r}")


# -- hallucinated / missing citations -------------------------------------


def test_unknown_evidence_id_is_never_verified(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=["code:pydantic:usage:doesnotexist", "doc:test:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    assert result.verified_risks == []
    risk = result.degraded_risks[0]
    assert risk.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert risk.unknown_evidence_ids == ["code:pydantic:usage:doesnotexist"]
    assert IssueCode.UNKNOWN_EVIDENCE_ID in {i.code for i in risk.issues}
    assert result.citation_existence_rate < 1.0


def test_risk_without_any_evidence_is_rejected(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    report = _report([RiskItem(risk_id="r1", title="something broke", evidence_ids=[])])

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    risk = result.degraded_risks[0]
    assert risk.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert IssueCode.NO_EVIDENCE_IDS in {i.code for i in risk.issues}


def test_doc_only_risk_has_no_code_backing(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    report = _report(
        [RiskItem(risk_id="r1", title="validator is removed", evidence_ids=["doc:test:1"])]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    risk = result.degraded_risks[0]
    assert IssueCode.NO_CODE_EVIDENCE in {i.code for i in risk.issues}
    assert risk.status is EvidenceStatus.INSUFFICIENT_EVIDENCE


# -- the happy path --------------------------------------------------------


def test_code_plus_official_doc_is_verified(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    code_id = _first_code_id(bundle, "validator")
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed in v2",
                severity="high",
                evidence_ids=[code_id, "doc:test:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    assert len(result.verified_risks) == 1
    risk = result.verified_risks[0]
    assert risk.status is EvidenceStatus.VERIFIED
    assert risk.code_evidence_ids == [code_id]
    assert risk.doc_evidence_ids == ["doc:test:1"]
    assert result.conclusion is Conclusion.IMPACTED
    assert result.citation_existence_rate == 1.0


def test_missing_doc_evidence_downgrades_to_partial(repo, scanned, skill):
    bundle = build_bundle(scanned, [], dependency="pydantic")
    code_id = _first_code_id(bundle, "validator")
    report = _report([RiskItem(risk_id="r1", title="validator is removed", evidence_ids=[code_id])])

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    risk = result.degraded_risks[0]
    assert risk.status is EvidenceStatus.PARTIALLY_VERIFIED
    assert IssueCode.NO_DOC_EVIDENCE in {i.code for i in risk.issues}


def test_network_sourced_doc_is_verified_not_downgraded(repo, scanned, skill):
    """S16: evidence fetched via online fallback (provenance=online_fallback)
    must verify a risk instead of being downgraded for 'low trust'."""
    bundle = build_bundle(scanned, [], dependency="pydantic")
    code_id = _first_code_id(bundle, "validator")
    net_doc = EvidenceItem(
        evidence_id="doc:net:1",
        kind="doc_chunk",
        summary="validator was replaced by field_validator",
        detail="In Pydantic v2 `validator` is deprecated in favour of `field_validator`.",
        meta={
            "source_id": "online:pydantic",
            "chunk_id": "c1",
            "provenance": "online_fallback",
            "trust_level": "community",
        },
    )
    bundle.add(net_doc)
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed in v2",
                severity="high",
                evidence_ids=[code_id, "doc:net:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    assert len(result.verified_risks) == 1
    risk = result.verified_risks[0]
    assert risk.status is EvidenceStatus.VERIFIED
    codes = {i.code for i in risk.issues}
    # Network origin is marked, but it does NOT downgrade the risk.
    assert IssueCode.DOC_SOURCE_NETWORK in codes
    assert IssueCode.DOC_SOURCE_UNTRUSTED not in codes
    assert result.conclusion is Conclusion.IMPACTED


# -- stale evidence --------------------------------------------------------


def test_edited_file_invalidates_evidence(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    code_id = _first_code_id(bundle, "validator")
    (repo / "src" / "models.py").write_text("# rewritten\n", encoding="utf-8")

    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=[code_id, "doc:test:1"],
            )
        ]
    )
    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    risk = result.degraded_risks[0]
    codes = {i.code for i in risk.issues}
    assert IssueCode.CONTENT_HASH_CHANGED in codes
    assert risk.status is EvidenceStatus.INSUFFICIENT_EVIDENCE


def test_deleted_file_invalidates_evidence(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    code_id = _first_code_id(bundle, "validator")
    (repo / "src" / "models.py").unlink()

    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=[code_id, "doc:test:1"],
            )
        ]
    )
    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    risk = result.degraded_risks[0]
    assert IssueCode.FILE_NOT_FOUND in {i.code for i in risk.issues}


# -- documentation version conflict ---------------------------------------


def test_doc_covering_a_different_major_is_a_conflict(repo, scanned, skill):
    bundle = build_bundle(scanned, [], dependency="pydantic")
    code_id = _first_code_id(bundle, "validator")
    bundle.add(_doc_item(skill))

    # The skill's doc source targets v2; assess against v1 instead.
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=[code_id, "doc:test:1"],
            )
        ],
        target_version_spec="1.10.2",
    )
    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    risk = result.degraded_risks[0]
    assert risk.status is EvidenceStatus.CONFLICTING_EVIDENCE
    assert IssueCode.DOC_VERSION_CONFLICT in {i.code for i in risk.issues}


# -- test recommendation ---------------------------------------------------


def test_related_test_is_recommended(repo, scanned, skill):
    bundle = _bundle_with_doc(scanned, skill)
    code_id = _first_code_id(bundle, "validator")
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=[code_id, "doc:test:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=skill)

    paths = [t.test_path for t in result.recommended_tests]
    assert "tests/test_models.py" in paths


def test_no_related_test_yields_empty_recommendation(tmp_path: Path, skill):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lonely.py").write_text(PRODUCTION, encoding="utf-8")
    scanned = scan_code_evidence(tmp_path, "pydantic")
    bundle = _bundle_with_doc(scanned, skill)
    code_id = _first_code_id(bundle, "validator")

    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=[code_id, "doc:test:1"],
            )
        ]
    )
    result = verify_report(
        report, repo_root=tmp_path, bundle=bundle, code_report=scanned, skill=skill
    )

    assert result.recommended_tests == []


# -- no impact -------------------------------------------------------------


def test_repository_without_usage_reports_no_impact(tmp_path: Path, skill):
    (tmp_path / "app.py").write_text("import os\n\nprint(os.getcwd())\n", encoding="utf-8")
    scanned = scan_code_evidence(tmp_path, "pydantic")
    bundle = build_bundle(scanned, [], dependency="pydantic")

    result = verify_report(
        _report([]), repo_root=tmp_path, bundle=bundle, code_report=scanned, skill=skill
    )

    assert result.conclusion is Conclusion.NO_IMPACT
    assert result.verified_risks == []
    assert result.degraded_risks == []
