"""Tests for S13: the modification plan and flattened assessment are default products.

Every ``upgrade_task`` run must yield ``AgentResult.upgrade_plan`` (an
``UpgradePlan``) and ``AgentResult.assessment`` (the presentation view), and the
run store must persist ``upgrade-plan.json`` / ``upgrade-plan.md``.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens import AgentResult, DependencyUpgradeAgent
from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeEvidenceSummary
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport
from upgradelens.pipeline import AssessmentOutcome
from upgradelens.plan import PlanMode, UpgradePlan, build_upgrade_plan
from upgradelens.report import render_plan_markdown
from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    VerifiedReport,
    VerifiedRisk,
)

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"
PYDANTIC_REPO = CASES_DIR / "pydantic_field_validator" / "repo"


def _make_outcome(repo_path: str, risk: VerifiedRisk) -> AssessmentOutcome:
    """Build a minimal AssessmentOutcome carrying one verified risk."""
    bundle = EvidenceBundle()
    bundle.add(
        EvidenceItem(
            evidence_id="code:e1",
            kind="code_usage",
            summary="old_func is called",
            detail="src/app.py uses old_func",
            meta={"path": "src/app.py", "symbol": "old_func"},
        )
    )
    verified = VerifiedReport(
        target_dependency="demo",
        source_version_spec="1.0",
        target_version_spec="2.0",
        verified_risks=[risk],
        degraded_risks=[],
        conclusion=Conclusion.IMPACTED,
    )
    report = ImpactReport(
        target_dependency="demo",
        source_version_spec="1.0",
        target_version_spec="2.0",
        risks=[],
    )
    return AssessmentOutcome(
        report=report,
        verified=verified,
        repo_path=repo_path,
        skill=None,
        bundle=bundle,
        code_report=CodeEvidenceReport(
            dependency_name="demo",
            scanned_files=1,
            summary=CodeEvidenceSummary(scanned_files=1, usage_count=0),
        ),
        degradations=(),
    )


def test_fake_run_yields_default_plan_and_assessment():
    """The plan + assessment are produced without an explicit --plan flag (S13)."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic from 1.x to 2.7",
        repo=str(PYDANTIC_REPO),
        dependency="pydantic",
        target_version="2.7",
    )
    assert isinstance(result, AgentResult)
    assert result.upgrade_plan is not None
    assert result.upgrade_plan.target_dependency == "pydantic"
    assert result.assessment is not None
    assert result.assessment.verdict in {
        "needs_upgrade",
        "evidence_insufficient",
        "no_risk",
        "no_impact",
    }


def test_plan_step_enrichment_from_verified_risk(tmp_path):
    """UpgradeStep fields are populated from VerifiedRisk (S13 enrichment)."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def old_func():\n    return 'old'\n", encoding="utf-8")
    risk = VerifiedRisk(
        risk_id="r1",
        title="Use new_func instead of old_func",
        status=EvidenceStatus.VERIFIED,
        severity="medium",
        model_severity="medium",
        code_evidence_ids=["code:e1"],
        doc_evidence_ids=["doc:d1"],
        recommendation="Replace old_func with new_func.",
        problem="old_func 在 2.0 中已被移除",
        behavior_change="调用方需改用 new_func",
        verification_steps=["运行 pytest 验证通过", "确认代码库中无 old_func 残留"],
        before_example="x = old_func()",
        after_example="x = new_func()",
    )
    outcome = _make_outcome(str(root), risk)
    plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.PATCH_DRAFT)

    assert isinstance(plan, UpgradePlan)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.change_reason == "old_func 在 2.0 中已被移除"
    assert step.before_example == "x = old_func()"
    assert step.after_example == "x = new_func()"
    assert step.completion_criteria == ["运行 pytest 验证通过", "确认代码库中无 old_func 残留"]
    # Feedback loop: the plan now derives target files from the risk's evidence.
    assert step.target_files == ["src/app.py"]
    assert step.api_symbols == ["old_func"]


def test_run_store_writes_plan_artifacts(tmp_path):
    """run() persists the plan as machine- and human-readable artifacts."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic from 1.x to 2.7",
        repo=str(PYDANTIC_REPO),
        dependency="pydantic",
        target_version="2.7",
        out_dir=tmp_path,
    )
    assert result.run_dir is not None
    run_dir = Path(result.run_dir)
    plan_json = run_dir / "upgrade-plan.json"
    plan_md = run_dir / "upgrade-plan.md"
    assert plan_json.exists()
    assert (run_dir / "assessment.json").exists()
    assert plan_md.exists()

    md_text = plan_md.read_text(encoding="utf-8")
    assert "修改计划" in md_text
    assert "pydantic" in md_text

    loaded = UpgradePlan.model_validate_json(plan_json.read_text(encoding="utf-8"))
    assert loaded.target_dependency == "pydantic"
    assert loaded.step_count == len(loaded.steps)


def test_render_plan_markdown_is_deterministic():
    """The Chinese 修改说明 renderer is pure and deterministic."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic from 1.x to 2.7",
        repo=str(PYDANTIC_REPO),
        dependency="pydantic",
        target_version="2.7",
    )
    assert result.upgrade_plan is not None
    first = render_plan_markdown(result.upgrade_plan)
    second = render_plan_markdown(result.upgrade_plan)
    assert first == second
    assert "修改计划" in first
