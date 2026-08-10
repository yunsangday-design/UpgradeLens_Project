"""S7: UpgradePlan schema, plan-only export, and controlled executor.

These tests are fully deterministic -- no model, no network, no real git. They build a
minimal :class:`AssessmentOutcome` and a throwaway repo on disk, then exercise the plan
builder and the sandbox executor end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from upgradelens.domain.code_evidence import (
    CodeEvidenceReport,
    CodeEvidenceSummary,
)
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport
from upgradelens.patch.models import PatchDraft, PatchFileDiff, PatchHunk
from upgradelens.pipeline import AssessmentOutcome
from upgradelens.plan import (
    ExecutionStatus,
    PlanMode,
    UpgradePlan,
    build_upgrade_plan,
    execute_plan,
    export_plan,
)
from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    VerifiedReport,
    VerifiedRisk,
)
from upgradelens.verify.models import (
    TestCandidate as Candidate,
)


def _make_outcome(repo_path: str, *, extra_risk: VerifiedRisk | None = None) -> AssessmentOutcome:
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
    risks = [
        VerifiedRisk(
            risk_id="r1",
            title="Use new_func instead of old_func",
            status=EvidenceStatus.VERIFIED,
            severity="medium",
            model_severity="medium",
            code_evidence_ids=["code:e1"],
            doc_evidence_ids=["doc:d1"],
            recommended_tests=[],
            recommendation="Replace old_func with new_func.",
        )
    ]
    if extra_risk is not None:
        risks.append(extra_risk)
    verified = VerifiedReport(
        target_dependency="demo",
        source_version_spec="1.0",
        target_version_spec="2.0",
        verified_risks=risks,
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


def _write_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(
        'import os\n'
        '\n'
        'def old_func():\n'
        '    return "old"\n'
        '\n'
        'x = old_func()\n',
        encoding="utf-8",
    )


def _render(hunk: PatchHunk) -> str:
    header = (
        f"@@ -{hunk.old_start},{hunk.old_count} "
        f"+{hunk.new_start},{hunk.new_count} @@\n"
    )
    return header + "\n".join(hunk.body) + "\n"


def _replace_all_hunk() -> PatchHunk:
    """Replace the whole old_func block (def + call) with new_func."""
    return PatchHunk(
        path="src/app.py",
        old_start=3,
        old_count=4,
        new_start=3,
        new_count=4,
        body=[
            "-def old_func():",
            '-    return "old"',
            "-",
            "-x = old_func()",
            "+def new_func():",
            '+    return "new"',
            "+",
            "+x = new_func()",
        ],
    )


def _replace_def_only_hunk() -> PatchHunk:
    """Replace the def but keep the (now stale) call -> old_func still present."""
    return PatchHunk(
        path="src/app.py",
        old_start=3,
        old_count=4,
        new_start=3,
        new_count=4,
        body=[
            "-def old_func():",
            '-    return "old"',
            "+def new_func():",
            '+    return "new"',
            " ",
            " x = old_func()",
        ],
    )


def _patch(hunk: PatchHunk) -> PatchDraft:
    return PatchDraft(
        dependency="demo",
        target_version_spec="2.0",
        files=[PatchFileDiff(path="src/app.py", hunks=[hunk], diff_text=_render(hunk))],
    )


# --------------------------------------------------------------------------- #
# Plan builder
# --------------------------------------------------------------------------- #


def test_build_upgrade_plan_projection() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        _write_repo(root)
        outcome = _make_outcome(str(root))
        plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.PATCH_DRAFT)

    assert isinstance(plan, UpgradePlan)
    assert plan.target_dependency == "demo"
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.step_id == "r1"
    assert step.target_files == ["src/app.py"]
    assert step.api_symbols == ["old_func"]
    assert step.change_reason == "Replace old_func with new_func."
    assert step.doc_evidence == ["doc:d1"]
    # Single step -> nothing is off-limits to it.
    assert step.forbidden_regions == []
    # Non-git temp dir -> repo hash anchored to empty.
    assert plan.repo_hash == ""


def test_build_upgrade_plan_forbids_cross_step_files() -> None:
    """A second step owning a different file makes that file forbidden to the first."""
    second = VerifiedRisk(
        risk_id="r2",
        title="Another change",
        status=EvidenceStatus.VERIFIED,
        severity="low",
        model_severity="low",
        code_evidence_ids=[],
        doc_evidence_ids=[],
        recommended_tests=[
            Candidate(
                test_path="tests/test_other.py",
                production_path="src/other.py",
                matched_by="name",
            )
        ],
        recommendation="edit tests/test_other.py",
    )
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        outcome = _make_outcome(str(root), extra_risk=second)
        plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.PATCH_DRAFT)

    assert {s.step_id for s in plan.steps} == {"r1", "r2"}
    by_id = {s.step_id: s for s in plan.steps}
    assert "tests/test_other.py" in by_id["r1"].forbidden_regions
    assert "src/app.py" in by_id["r2"].forbidden_regions


# --------------------------------------------------------------------------- #
# plan-only export
# --------------------------------------------------------------------------- #


def test_export_plan_round_trip() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        _write_repo(root)
        outcome = _make_outcome(str(root))
        plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.PATCH_DRAFT)
        dest = export_plan(plan, root / "upgrade.plan.json")

        data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["schema_version"] == "upgrade-plan/1"
    assert data["steps"][0]["api_symbols"] == ["old_func"]
    # The execution contract spells out the apply rules.
    assert "Apply only at the recorded repo_hash." in plan.to_execution_contract()["rules"]


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


def test_execute_patch_draft_does_not_mutate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    outcome = _make_outcome(str(root))
    plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.PATCH_DRAFT)
    # Attach a concrete patch so the diff is non-empty.
    plan = plan.model_copy(update={"patch": _patch(_replace_all_hunk())})

    result = execute_plan(plan, repo_root=root)

    assert result.status == ExecutionStatus.SKIPPED
    assert "old_func" in result.diff
    assert result.modified_files == []
    # The real workspace is untouched.
    assert "old_func" in (root / "src" / "app.py").read_text(encoding="utf-8")


def test_execute_sandbox_apply_removes_old_api(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    sandbox = tmp_path / "sandbox"
    root.mkdir()
    _write_repo(root)
    outcome = _make_outcome(str(root))
    plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.SANDBOX_APPLY)
    plan = plan.model_copy(update={"patch": _patch(_replace_all_hunk())})

    result = execute_plan(plan, repo_root=root, work_dir=sandbox)

    assert result.status == ExecutionStatus.ACCEPTED
    assert result.modified_files == ["src/app.py"]
    assert result.old_api_residual == []
    assert result.evidence_coverage == 1.0
    assert result.unrelated_modifications == []
    # Original workspace unchanged.
    assert "old_func" in (root / "src" / "app.py").read_text(encoding="utf-8")
    # Sandbox shows the fix.
    sandbox_text = (sandbox / "src" / "app.py").read_text(encoding="utf-8")
    assert "new_func" in sandbox_text
    assert "old_func" not in sandbox_text


def test_execute_sandbox_apply_flags_residual(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    sandbox = tmp_path / "sandbox"
    root.mkdir()
    _write_repo(root)
    outcome = _make_outcome(str(root))
    plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.SANDBOX_APPLY)
    # Patch replaces the def but leaves the call -> old_func still present.
    plan = plan.model_copy(update={"patch": _patch(_replace_def_only_hunk())})

    result = execute_plan(plan, repo_root=root, work_dir=sandbox)

    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert result.old_api_residual == ["old_func"]
    assert result.evidence_coverage == 0.0


def test_execute_rejects_repo_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    sandbox = tmp_path / "sandbox"
    root.mkdir()
    _write_repo(root)
    outcome = _make_outcome(str(root))
    plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.SANDBOX_APPLY)
    plan = plan.model_copy(
        update={"patch": _patch(_replace_all_hunk()), "repo_hash": "deadbeef"}
    )

    result = execute_plan(plan, repo_root=root, work_dir=sandbox)

    assert result.status == ExecutionStatus.REJECTED
    assert "repo hash mismatch" in result.notes


def test_execute_skips_without_patch_draft(tmp_path: Path) -> None:
    """Phase-1 sandbox apply with no draft is skipped, never faked."""
    root = tmp_path / "repo"
    sandbox = tmp_path / "sandbox"
    root.mkdir()
    _write_repo(root)
    outcome = _make_outcome(str(root))
    plan = build_upgrade_plan(outcome, repo_root=root, mode=PlanMode.SANDBOX_APPLY)
    # No patch attached (capability pack absent in real runs).

    result = execute_plan(plan, repo_root=root, work_dir=sandbox)

    assert result.status == ExecutionStatus.SKIPPED
    assert "no patch draft" in result.notes
