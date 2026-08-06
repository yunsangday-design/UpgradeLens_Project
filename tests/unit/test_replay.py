"""Record → replay round-trip for the model gateway.

The LLM integration (Stage 5) supports a ``replay`` mode that replays recorded
node responses fully offline. This test proves the **recording** path writes one
file per node name (``planner``, ``extractor__<pattern_id>``, ``impact_analyzer``)
and that a subsequent ``replay`` run reproduces the exact same report -- all
without any API key. Real model output is captured the same way via
``--record-replay`` in live mode.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.analyzers import scan_code_evidence
from upgradelens.graph import AssessmentSpec, run_assessment
from upgradelens.llm import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import (
    BreakingChange,
    ImpactReport,
    Plan,
    PlanItem,
    RiskItem,
    build_bundle,
)
from upgradelens.skills import builtin_registry


def _build_fakes(code_id: str) -> dict[str, object]:
    return {
        "planner": Plan(items=[PlanItem(pattern_id="demo1", question="q")]),
        "extractor__demo1": BreakingChange(
            pattern_id="demo1",
            title="demo breaking change",
            severity="high",
            evidence_ids=[code_id],
        ),
        "impact_analyzer": ImpactReport(
            target_dependency="pydantic",
            risks=[
                RiskItem(
                    risk_id="r1",
                    title="demo risk",
                    severity="high",
                    confidence="high",
                    evidence_ids=[code_id],
                    recommendation="review",
                )
            ],
        ),
    }


def test_record_then_replay_reproduces_report(tmp_path: Path) -> None:
    repo = Path("tests/fixtures/eval/validator_direct_hit/repo")
    dep = "pydantic"
    tv = "2.0.0"

    cr = scan_code_evidence(str(repo), dep)
    bundle = build_bundle(cr, dependency=dep)
    registry = builtin_registry()
    skill = registry.get(registry.select_skill(dep, tv).skill_id)
    code_id = next(i.evidence_id for i in bundle.items if i.kind == "code_usage")

    spec = AssessmentSpec(repo=str(repo), dependency=dep, target_version_spec=f"=={tv}")

    # 1) Record a (fake) run to disk.
    rec_dir = tmp_path / "rec"
    gw_rec = ModelGateway(
        ModelConfig(mode=ModelMode.FAKE, max_total_tokens=2000),
        fake_responses=_build_fakes(code_id),
        recording_dir=str(rec_dir),
    )
    recorded = run_assessment(spec, bundle, gw_rec, skill=skill)

    # One file per node name, each wrapping the output under "output".
    assert (rec_dir / "planner.json").exists()
    assert (rec_dir / "extractor__demo1.json").exists()
    assert (rec_dir / "impact_analyzer.json").exists()
    import json

    payload = json.loads((rec_dir / "impact_analyzer.json").read_text(encoding="utf-8"))
    assert "output" in payload

    # 2) Replay the recording fully offline.
    gw_replay = ModelGateway(
        ModelConfig(mode=ModelMode.REPLAY, max_total_tokens=2000), replay_dir=str(rec_dir)
    )
    replayed = run_assessment(spec, bundle, gw_replay, skill=skill)

    assert [r.risk_id for r in replayed.risks] == [r.risk_id for r in recorded.risks] == ["r1"]
    assert replayed.risks[0].evidence_ids == [code_id]
