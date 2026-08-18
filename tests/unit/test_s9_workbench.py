"""Tests for the unified Workbench (plan stage S9).

These exercise :func:`run_capability`, the single entry point that runs any
registered capability and normalises it into a capability-agnostic
:class:`CapabilityRunResult`. All runs use the bundled ``fake`` responses, so the
suite is fully offline.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities.workbench import (
    CapabilityRunResult,
    list_capabilities,
    run_capability,
)
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_FIXTURE_REPO = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/eval/pydantic_field_validator/repo"
)
REPO = str(_FIXTURE_REPO.resolve())
DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
)


def _task(kind: str, **kw: object) -> SoftwareTask:
    goal = str(kw.pop("goal", ""))
    repo = str(kw.pop("repo", REPO))
    return SoftwareTask(
        task_id="t",
        kind=TaskKind(kind),
        goal=goal,
        context=TaskContext(repo=repo, **kw),
    )


def test_list_capabilities_exposes_all_kinds() -> None:
    kinds = {c["kind"] for c in list_capabilities()}
    assert {
        "dependency_upgrade",
        "pr_review",
        "issue_repair",
        "security_review",
        "breaking_change",
    } <= kinds


def test_run_capability_returns_normalized_model() -> None:
    result = run_capability(_task("pr_review", unified_diff=DIFF))
    assert isinstance(result, CapabilityRunResult)
    dumped = result.model_dump(mode="json")
    assert dumped["capability"] == "pr_review"
    assert isinstance(dumped["findings"], list)


def test_pr_review_normalizes_findings_and_verification() -> None:
    result = run_capability(_task("pr_review", unified_diff=DIFF))
    assert result.status == "succeeded"
    assert result.findings, "pr_review should surface findings"
    first = result.findings[0]
    assert {"finding_id", "severity", "summary"} <= set(first.keys())
    assert result.verification is not None
    assert result.verification.get("passed") is True
    assert result.test_results, "pr_review should recommend at least one test"


def test_issue_repair_normalizes_patch_and_repro_tests() -> None:
    result = run_capability(_task("issue_repair", issue_text="app crashes on startup"))
    assert result.status == "succeeded"
    assert result.findings, "issue_repair should surface findings"
    assert result.patch, "issue_repair should propose a patch"
    assert result.test_results, "issue_repair should propose a repro test"


def test_security_review_normalizes_coverage_and_report() -> None:
    result = run_capability(_task("security_review", unified_diff=DIFF, dependency="pydantic"))
    assert result.status == "succeeded"
    assert result.findings
    assert result.coverage, "security_review should report coverage"
    assert result.security_results, "security_review should include the report"
    assert result.verification is not None


def test_breaking_change_normalizes_findings() -> None:
    result = run_capability(
        _task(
            "breaking_change",
            unified_diff=DIFF,
            from_version="1.10",
            to_version="2.7",
        )
    )
    assert result.status == "succeeded"
    assert result.findings
    assert result.verification is not None


def test_dependency_upgrade_normalizes_patch_and_verification() -> None:
    result = run_capability(
        _task(
            "dependency_upgrade",
            goal="upgrade pydantic to 2.7",
            dependency="pydantic",
            source_version="1.10",
            target_version="2.7",
        )
    )
    assert result.status == "succeeded"
    assert result.patch, "dependency_upgrade should surface a patch plan"
    assert result.verification is not None


def test_failure_is_surfaced_not_raised() -> None:
    # A non-existent repo still yields a failed (not crashed) result for the
    # capability kinds that read from disk.
    result = run_capability(_task("pr_review", repo="/no/such/repo/here", unified_diff=DIFF))
    assert result.status in {"succeeded", "failed"}
    if result.status == "failed":
        assert result.error
