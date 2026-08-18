"""Unified task dispatch (research report M1b)."""

from __future__ import annotations

from upgradelens.agent.dispatch import dispatch_by_task
from upgradelens.capabilities.workbench import run_capability
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_REPO = (
    "tests/fixtures/eval/pydantic_field_validator/repo"
)


def _task(kind: TaskKind, **ctx) -> SoftwareTask:
    return SoftwareTask(
        task_id="dispatch-test",
        kind=kind,
        goal="dispatch smoke test",
        context=TaskContext(repo=_REPO, **ctx),
    )


def test_dispatch_by_task_sets_capability_meta():
    task = _task(TaskKind.PR_REVIEW, unified_diff="diff --git a/src/app.py b/src/app.py\n")
    result = dispatch_by_task(task, mode="fake")
    assert result.capability == "pr_review"
    assert isinstance(result.capability_meta, dict)
    assert result.capability_meta["kind"] == "pr_review"
    assert "allowed_tools" in result.capability_meta


def test_dispatch_by_task_matches_direct_run_capability():
    # dispatch_by_task is a thin unified brain over run_capability; the result
    # shape must be identical for the same input.
    task = _task(TaskKind.SECURITY_REVIEW, unified_diff="diff --git a/src/app.py b/src/app.py\n")
    via_dispatch = dispatch_by_task(task, mode="fake")
    via_direct = run_capability(task, mode="fake")
    assert via_dispatch.capability == via_direct.capability
    assert via_dispatch.findings == via_direct.findings
    assert via_dispatch.verification == via_direct.verification
