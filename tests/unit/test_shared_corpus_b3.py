"""Stage B3 -- analysis and verification no longer need a dedicated Skill Pack.

Every test here runs the *real* scanner against a miniature repository and then
drives planning, scoring and verification with ``skill=None``. That is the whole
point of the stage: the quality of the analysis must come from evidence (code
symbols + retrieved documentation), not from a hand-curated pack, so a
dependency without a pack must not be analysed any more weakly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from upgradelens.analyzers import scan_code_evidence
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import ingest
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.graph.nodes import planner
from upgradelens.graph.state import AssessmentSpec
from upgradelens.models.impact import (
    EvidenceBundle,
    EvidenceItem,
    ImpactReport,
    Plan,
    RiskItem,
    build_bundle,
)
from upgradelens.pipeline import AssessmentRequest, collect_evidence
from upgradelens.skills import builtin_registry
from upgradelens.tools.registry import ToolContext, default_registry
from upgradelens.verify import EvidenceStatus, IssueCode, verify_report
from upgradelens.verify.risk_rules import RiskScoringInput, score_risk

PRODUCTION = """\
from pydantic import BaseModel, validator


class User(BaseModel):
    name: str

    @validator("name")
    def check(cls, v):
        return v
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "models.py").write_text(PRODUCTION, encoding="utf-8")
    return tmp_path


@pytest.fixture
def scanned(repo: Path) -> CodeEvidenceReport:
    return scan_code_evidence(repo, "pydantic")


def _doc_item(
    *,
    evidence_id: str = "doc:corpus:1",
    summary: str = "validator was replaced by field_validator",
    detail: str = "In Pydantic v2 `validator` is replaced by `field_validator`.",
    target_version_spec: str = ">=2,<3",
    trust_level: str = "official",
    chunk_title: str = "Validators",
) -> EvidenceItem:
    """A shared-corpus chunk: version window and trust live on the evidence."""
    return EvidenceItem(
        evidence_id=evidence_id,
        kind="doc_chunk",
        summary=summary,
        detail=detail,
        meta={
            "source_id": "src:pydantic:migration",
            "chunk_id": "c1",
            "chunk_title": chunk_title,
            "package_name": "pydantic",
            "source_version_spec": ">=1,<2",
            "target_version_spec": target_version_spec,
            "trust_level": trust_level,
        },
    )


def _bundle(scanned: CodeEvidenceReport, *docs: EvidenceItem) -> EvidenceBundle:
    bundle = build_bundle(scanned, [], dependency="pydantic")
    for doc in docs:
        bundle.add(doc)
    return bundle


def _code_id(bundle: EvidenceBundle, symbol: str) -> str:
    for item in bundle.by_kind("code_usage"):
        if item.meta.get("symbol") == symbol:
            return item.evidence_id
    raise AssertionError(f"no code evidence for symbol {symbol!r}")


def _report(risks: list[RiskItem], **kw: Any) -> ImpactReport:
    payload: dict[str, Any] = {
        "target_dependency": "pydantic",
        "source_version_spec": "1.10",
        "target_version_spec": "2.7",
        "risks": risks,
    }
    payload.update(kw)
    return ImpactReport(**payload)


# -- planner ---------------------------------------------------------------


class _PromptCapturingGateway:
    """Records the prompt instead of calling a model."""

    def __init__(self) -> None:
        self.prompt = ""

    def complete_structured(self, *, prompt: str, schema: Any, name: str) -> tuple[Any, None]:
        self.prompt = prompt
        return schema(), None


def test_planner_plans_from_evidence_without_a_skill(scanned: CodeEvidenceReport) -> None:
    bundle = _bundle(scanned, _doc_item())
    gateway = _PromptCapturingGateway()

    result = planner(
        {
            "spec": AssessmentSpec(repo=".", dependency="pydantic", target_version_spec="2.7"),
            "bundle": bundle,
            # no "skill" key at all -- planning must not need one
        },  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
    )

    assert isinstance(result["plan"], Plan)
    # The two evidence-derived signals reached the prompt...
    assert "- validator" in gateway.prompt
    assert "- BaseModel" in gateway.prompt
    assert "[doc:corpus:1] Validators" in gateway.prompt
    # ...and no skill-pattern placeholder survived.
    assert "no skill" not in gateway.prompt
    assert "$" not in gateway.prompt


def test_planner_states_plainly_when_evidence_is_empty(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
    empty = scan_code_evidence(tmp_path, "pydantic")
    gateway = _PromptCapturingGateway()

    planner(
        {
            "spec": AssessmentSpec(repo=".", dependency="pydantic", target_version_spec="2.7"),
            "bundle": build_bundle(empty, [], dependency="pydantic"),
        },  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
    )

    assert "(no API symbols found)" in gateway.prompt
    assert "(no documentation retrieved)" in gateway.prompt


# -- verifier: version window and trust come from the evidence -------------


def test_doc_version_conflict_is_detected_without_a_skill(
    repo: Path, scanned: CodeEvidenceReport
) -> None:
    """The chunk's own ``target_version_spec`` is enough to spot the mismatch."""
    bundle = _bundle(scanned, _doc_item())
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed",
                evidence_ids=[_code_id(bundle, "validator"), "doc:corpus:1"],
            )
        ],
        target_version_spec="1.10.2",
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=None)

    risk = result.degraded_risks[0]
    assert risk.status is EvidenceStatus.CONFLICTING_EVIDENCE
    assert IssueCode.DOC_VERSION_CONFLICT in {i.code for i in risk.issues}


def test_matching_doc_version_verifies_without_a_skill(
    repo: Path, scanned: CodeEvidenceReport
) -> None:
    bundle = _bundle(scanned, _doc_item())
    code_id = _code_id(bundle, "validator")
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed in v2",
                evidence_ids=[code_id, "doc:corpus:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=None)

    assert len(result.verified_risks) == 1
    assert result.verified_risks[0].status is EvidenceStatus.VERIFIED


# -- verifier: symbol grounding uses the evidence vocabulary ---------------


def test_title_symbol_must_match_the_cited_code_without_a_skill(
    repo: Path, scanned: CodeEvidenceReport
) -> None:
    """Citing ``BaseModel`` for a risk titled about ``validator`` is misgrounded."""
    bundle = _bundle(scanned, _doc_item())
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="validator is removed in v2",
                evidence_ids=[_code_id(bundle, "BaseModel"), "doc:corpus:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=None)

    risk = result.degraded_risks[0]
    assert IssueCode.SYMBOL_NOT_IN_EVIDENCE in {i.code for i in risk.issues}


def test_grounding_matches_whole_identifiers_only(repo: Path, scanned: CodeEvidenceReport) -> None:
    """``field_validator`` in a title does not count as a mention of ``validator``."""
    bundle = _bundle(scanned, _doc_item())
    report = _report(
        [
            RiskItem(
                risk_id="r1",
                title="field_validator replaces the old decorator",
                evidence_ids=[_code_id(bundle, "BaseModel"), "doc:corpus:1"],
            )
        ]
    )

    result = verify_report(report, repo_root=repo, bundle=bundle, code_report=scanned, skill=None)

    all_issues = {i.code for r in result.verified_risks + result.degraded_risks for i in r.issues}
    assert IssueCode.SYMBOL_NOT_IN_EVIDENCE not in all_issues


# -- scoring: doc grounding replaces skill pattern severity ----------------


def _factor(factors: list[Any], name: str) -> Any:
    for factor in factors:
        if factor.name == name:
            return factor
    raise AssertionError(f"missing factor {name!r}")


def test_docs_naming_the_used_symbol_score_highest_without_a_skill(
    scanned: CodeEvidenceReport,
) -> None:
    bundle = _bundle(scanned, _doc_item())
    code_items = [i for i in bundle.by_kind("code_usage") if i.meta.get("symbol") == "validator"]

    _, _, factors = score_risk(
        RiskScoringInput(
            status=EvidenceStatus.VERIFIED,
            code_items=code_items,
            doc_items=list(bundle.by_kind("doc_chunk")),
            skill=None,
            source_version_spec="1.10",
            target_version_spec="2.7",
            risk_title="validator is removed in v2",
        )
    )

    grounding = _factor(factors, "doc_symbol_grounding")
    assert grounding.points == 3
    assert "validator" in grounding.value
    # Trust is read off the chunk meta, with no skill to fall back on.
    assert _factor(factors, "doc_trust").value == "official"
    assert _factor(factors, "api_change_kind").points == 2


def test_unrelated_docs_score_lower_than_grounded_docs(scanned: CodeEvidenceReport) -> None:
    unrelated = _doc_item(
        evidence_id="doc:corpus:2",
        summary="Installation",
        detail="Install the package with pip.",
        chunk_title="Installation",
    )
    bundle = _bundle(scanned, unrelated)
    code_items = [i for i in bundle.by_kind("code_usage") if i.meta.get("symbol") == "validator"]

    _, _, factors = score_risk(
        RiskScoringInput(
            status=EvidenceStatus.VERIFIED,
            code_items=code_items,
            doc_items=list(bundle.by_kind("doc_chunk")),
            skill=None,
            source_version_spec="1.10",
            target_version_spec="2.7",
            risk_title="something may change",
        )
    )

    assert _factor(factors, "doc_symbol_grounding").points == 1
    assert _factor(factors, "api_change_kind").points == 0


def test_missing_skill_pack_is_not_reported_as_a_degradation(monkeypatch, tmp_path: Path) -> None:
    """A missing pack costs no capability, so it must not be announced as one."""
    db = tmp_path / "docs.db"
    engine = engine_for(db)
    init_db(engine)
    session = session_for(engine)()
    try:
        for pack in builtin_registry().all():
            ingest.ingest_skill(session, pack)
    finally:
        session.close()

    monkeypatch.setattr(
        "upgradelens.tools.registry.resolve_skill_package",
        lambda *args, **kwargs: None,
    )
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "models.py").write_text(PRODUCTION, encoding="utf-8")

    ctx = ToolContext(workdir=tmp_path)
    try:
        collection = collect_evidence(
            AssessmentRequest(
                repo=str(repo_dir), dependency="pydantic", target_version="2.0", db=db
            ),
            ctx,
            registry=default_registry(),
        )
    finally:
        ctx.close()

    assert collection.skill is None
    assert not any("Skill Pack" in note for note in collection.degradations)


def test_no_documentation_scores_nothing_for_grounding(scanned: CodeEvidenceReport) -> None:
    bundle = _bundle(scanned)

    _, _, factors = score_risk(
        RiskScoringInput(
            status=EvidenceStatus.PARTIALLY_VERIFIED,
            code_items=list(bundle.by_kind("code_usage")),
            doc_items=[],
            skill=None,
            source_version_spec="1.10",
            target_version_spec="2.7",
            risk_title="validator is removed",
        )
    )

    assert _factor(factors, "doc_symbol_grounding").points == 0
    assert _factor(factors, "doc_trust").value == "no doc evidence"
