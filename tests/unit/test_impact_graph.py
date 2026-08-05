"""Integration tests for the stage 5 assessment graph (offline, with the
pydantic fixture repo and an in-memory documentation index).
"""

from __future__ import annotations

import json
from pathlib import Path

from upgradelens.analyzers import scan_code_evidence
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import ingest_skill
from upgradelens.graph import (
    AssessmentSpec,
    retrieve_skill_evidence,
    run_assessment,
)
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import (
    BreakingChange,
    ImpactReport,
    Plan,
    PlanItem,
    RiskItem,
    build_bundle,
)
from upgradelens.skills import builtin_registry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pydantic_usage"


def _make_bundle(tmp_path: Path):
    skill = builtin_registry().get("pydantic_v1_to_v2")
    assert skill is not None
    code_report = scan_code_evidence(FIXTURE, "pydantic")
    db = tmp_path / "assess.db"
    engine = engine_for(str(db))
    init_db(engine)
    session = session_for(engine)()
    try:
        ingest_skill(session, skill)
        doc_evidences = retrieve_skill_evidence(session, skill)
    finally:
        session.close()
    bundle = build_bundle(code_report, doc_evidences, dependency="pydantic")
    return skill, bundle


def test_fake_assessment_references_real_evidence(tmp_path: Path) -> None:
    skill, bundle = _make_bundle(tmp_path)
    assert bundle.items, "expected evidence in the bundle"

    ref_id = bundle.items[0].evidence_id
    fake = {
        "planner": Plan(
            items=[PlanItem(pattern_id="validator", question="does @validator still work?")]
        ),
        "extractor__validator": BreakingChange(
            pattern_id="validator",
            title="validator deprecation",
            detail="Use field_validator instead.",
            severity="high",
            evidence_ids=[ref_id],
        ),
        "impact_analyzer": ImpactReport(
            risks=[
                RiskItem(
                    risk_id="risk:1",
                    title="validator usage may break on upgrade",
                    severity="high",
                    confidence="high",
                    evidence_ids=[ref_id],
                    recommendation="Migrate @validator to @field_validator.",
                )
            ]
        ),
    }
    gateway = ModelGateway(ModelConfig(mode=ModelMode.FAKE), fake_responses=fake)
    spec = AssessmentSpec(
        repo=str(FIXTURE),
        dependency="pydantic",
        target_version_spec=skill.target_version_spec,
        source_version_spec="1.x",
    )
    report = run_assessment(spec, bundle, gateway, skill=skill)

    assert report.static is False
    assert report.risks, "expected at least one risk"
    for risk in report.risks:
        for eid in risk.evidence_ids:
            assert eid in bundle.ids, f"risk references unknown evidence {eid}"


def test_impact_analyzer_drops_unknown_evidence(tmp_path: Path) -> None:
    skill, bundle = _make_bundle(tmp_path)
    fake = {
        "planner": Plan(),
        "impact_analyzer": ImpactReport(
            risks=[
                RiskItem(
                    risk_id="r1",
                    title="phantom risk",
                    evidence_ids=["this_id_does_not_exist"],
                )
            ]
        ),
    }
    gateway = ModelGateway(ModelConfig(mode=ModelMode.FAKE), fake_responses=fake)
    spec = AssessmentSpec(repo=str(FIXTURE), dependency="pydantic", target_version_spec="2.0")
    report = run_assessment(spec, bundle, gateway, skill=skill)

    assert report.static is False
    assert report.risks == []
    assert "dropped" in report.notes.lower()


def test_replay_assessment(tmp_path: Path) -> None:
    skill, bundle = _make_bundle(tmp_path)
    ref_id = bundle.items[0].evidence_id

    replay_dir = tmp_path / "llm_replay"
    replay_dir.mkdir()
    (replay_dir / "planner.json").write_text(
        json.dumps({"output": {"items": [{"pattern_id": "validator", "question": "q"}]}}),
        encoding="utf-8",
    )
    (replay_dir / "extractor__validator.json").write_text(
        json.dumps(
            {
                "output": {
                    "pattern_id": "validator",
                    "title": "bc",
                    "detail": "",
                    "severity": "high",
                    "evidence_ids": [ref_id],
                }
            }
        ),
        encoding="utf-8",
    )
    (replay_dir / "impact_analyzer.json").write_text(
        json.dumps(
            {
                "output": {
                    "risks": [
                        {
                            "risk_id": "risk:1",
                            "title": "t",
                            "severity": "high",
                            "confidence": "high",
                            "evidence_ids": [ref_id],
                            "recommendation": "r",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    gateway = ModelGateway(ModelConfig(mode=ModelMode.REPLAY), replay_dir=str(replay_dir))
    spec = AssessmentSpec(
        repo=str(FIXTURE),
        dependency="pydantic",
        target_version_spec=skill.target_version_spec,
    )
    report = run_assessment(spec, bundle, gateway, skill=skill)

    assert report.static is False
    for risk in report.risks:
        for eid in risk.evidence_ids:
            assert eid in bundle.ids
