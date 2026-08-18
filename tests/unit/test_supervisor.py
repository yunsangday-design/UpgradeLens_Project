"""Tests for the controlled Supervisor + Handoff multi-agent layer (M3).

These run entirely in ``fake`` mode: no network, no API key. They assert the
orchestration *control flow* (single short-circuit vs. multi-agent fan-out), the
permission gate, and decomposition determinism -- exactly the hard constraints
the research report lays out for "Don't-Build-Multi-Agents" safe multi-agent use.
"""

from pathlib import Path

from upgradelens.agent.supervisor import (
    AgentContext,
    classify_capabilities,
    decompose_task,
    run_supervisor,
)
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_REPO = str(
    (
        Path(__file__).resolve().parent.parent.parent
        / "tests"
        / "fixtures"
        / "eval"
        / "capabilities"
        / "repo"
    ).as_posix()
)
_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@\n"
    "-    a = 1\n"
    "+    a = 2\n"
)


def _task(goal: str, kind: TaskKind, **ctx: str) -> SoftwareTask:
    context = TaskContext(repo=_REPO, unified_diff=_DIFF, **ctx)
    return SoftwareTask(task_id="t1", kind=kind, goal=goal, context=context)


def test_single_capability_short_circuits_to_dispatch():
    task = _task("review 这个 PR https://github.com/x/y", TaskKind.PR_REVIEW)
    sup = run_supervisor(task, AgentContext(mode="fake"), mode="fake")
    # Single capability => NO pointless multi-agent graph; result stays populated.
    assert sup.orchestration == "single"
    assert sup.capability_kinds == [TaskKind.PR_REVIEW.value]
    assert sup.result is not None
    assert len(sup.sub_results) == 1
    assert sup.sub_results[0].capability == TaskKind.PR_REVIEW.value


def test_multi_capability_fans_out_to_isolated_subagents():
    # Same request text hits BOTH the PR-review and security-review keywords.
    goal = "review 这个 PR https://github.com/x/y 并做安全扫描"
    task = _task(goal, TaskKind.PR_REVIEW)
    sup = run_supervisor(task, AgentContext(mode="fake"), mode="fake")
    assert sup.orchestration == "multi-agent"
    assert set(sup.capability_kinds) == {
        TaskKind.PR_REVIEW.value,
        TaskKind.SECURITY_REVIEW.value,
    }
    # Each capability ran in its own isolated sub-result; no result is shared.
    assert len(sup.sub_results) == 2
    kinds_seen = {r.capability for r in sup.sub_results}
    assert kinds_seen == {TaskKind.PR_REVIEW.value, TaskKind.SECURITY_REVIEW.value}


def test_permission_gate_drops_unauthorized_capability():
    goal = "review 这个 PR https://github.com/x/y 并做安全扫描"
    task = _task(goal, TaskKind.PR_REVIEW)
    ctx = AgentContext(mode="fake", allowed_capabilities=("pr_review",))
    sup = run_supervisor(task, ctx, mode="fake")
    # Security is not authorised => dropped before any handoff => single PR run.
    assert sup.orchestration == "single"
    assert sup.capability_kinds == [TaskKind.PR_REVIEW.value]


def test_no_capability_matched_is_not_a_crash():
    task = _task("今天天气不错", TaskKind.UNKNOWN)
    sup = run_supervisor(task, AgentContext(mode="fake"), mode="fake")
    assert sup.orchestration == "single"
    assert sup.result is None
    assert sup.verification_passed is False
    assert "no-capability-matched" in sup.degradations


def test_decompose_is_deterministic_and_multi_aware():
    goal = "review 这个 PR https://github.com/x/y 并做安全扫描"
    task = _task(goal, TaskKind.PR_REVIEW)
    ctx = AgentContext(mode="fake")
    a = decompose_task(task, ctx)
    b = decompose_task(task, ctx)
    assert a == b  # deterministic, offline-reproducible
    assert TaskKind.PR_REVIEW in a
    assert TaskKind.SECURITY_REVIEW in a


def test_router_classify_capabilities_multi():
    kinds = classify_capabilities(
        "review 这个 PR https://github.com/x/y 并做安全扫描",
        repo="https://github.com/x/y",
    )
    assert TaskKind.PR_REVIEW in kinds
    assert TaskKind.SECURITY_REVIEW in kinds


def test_router_classify_capabilities_single_issue():
    # Issue/bug keywords need no repo, and must not be shadowed by PR/security.
    kinds = classify_capabilities("修复这个 bug")
    assert kinds == [TaskKind.ISSUE_REPAIR]
