"""Tests for the Supervisor's unified-runtime multi-agent path (MA-2-2 / MA-2-3).

Multi-capability requests now fan out through the unified runtime: an
``ExecutionPlan`` DAG schedules the capability leaves as one parallel wave of
child runs under a shared ``BudgetLedger``, then the ``EvidenceReviewerAgent``
converges (fan-in) with de-duplication, conflict surfacing and the evidence
gate. These tests pin that wiring -- all in ``fake`` mode, offline-reproducible.
"""

from pathlib import Path

from upgradelens.agent.supervisor import AgentContext, run_supervisor
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


def _multi_task() -> SoftwareTask:
    goal = "review 这个 PR https://github.com/x/y 并做安全扫描"
    context = TaskContext(repo=_REPO, unified_diff=_DIFF)
    return SoftwareTask(task_id="t-multi", kind=TaskKind.PR_REVIEW, goal=goal, context=context)


def test_multi_agent_runs_through_unified_runtime():
    sup = run_supervisor(_multi_task(), AgentContext(mode="fake"), mode="fake")
    assert sup.orchestration == "multi-agent"
    assert set(sup.capability_kinds) == {TaskKind.PR_REVIEW.value, TaskKind.SECURITY_REVIEW.value}

    # unified-runtime observability is populated
    assert sup.root_run_id
    assert sup.execution_plan is not None
    step_ids = sorted(sup.execution_plan["steps"])
    assert step_ids == ["cap-0-security_review", "cap-1-pr_review"]
    assert all(
        s["strategy"] == "parallel" for s in sup.execution_plan["steps"].values()
    )

    # agent_runs: 2 capability leaves + 1 evidence-reviewer fan-in
    assert len(sup.agent_runs) == 3
    leaves = [r for r in sup.agent_runs if r["kind"] != "evidence_reviewer"]
    fan_in = [r for r in sup.agent_runs if r["kind"] == "evidence_reviewer"]
    assert len(leaves) == 2 and len(fan_in) == 1
    # every leaf is a child run of the supervisor root
    assert all(r["parent_run_id"] == sup.root_run_id for r in leaves)

    # aggregate result carries the shared evidence gate
    assert sup.aggregate_result is not None
    assert sup.aggregate_result["kind"] == "evidence_reviewer"
    assert "evidence-gated" in sup.aggregate_result["summary"]

    # legacy Workbench contract stays intact
    assert len(sup.sub_results) == 2
    assert {r.capability for r in sup.sub_results} == set(sup.capability_kinds)


def test_multi_agent_plan_ids_are_deterministic():
    a = run_supervisor(_multi_task(), AgentContext(mode="fake"), mode="fake")
    b = run_supervisor(_multi_task(), AgentContext(mode="fake"), mode="fake")
    assert a.capability_kinds == b.capability_kinds
    assert sorted(a.execution_plan["steps"]) == sorted(b.execution_plan["steps"])
    # run ids are fresh per invocation (two independent supervisor runs)
    assert a.root_run_id != b.root_run_id


def test_budget_ledger_sums_leaf_costs():
    sup = run_supervisor(_multi_task(), AgentContext(mode="fake"), mode="fake")
    leaf_total = sum(
        r["cost"]["total_tokens"]
        for r in sup.agent_runs
        if r["kind"] != "evidence_reviewer"
    )
    assert sup.budget_tokens_used == leaf_total
    assert sup.budget_tokens_limit == AgentContext(mode="fake").budget_tokens


def test_evidence_gate_on_aggregate_findings():
    sup = run_supervisor(_multi_task(), AgentContext(mode="fake"), mode="fake")
    assert sup.aggregate_result is not None
    for f in sup.aggregate_result["findings"]:
        if f["status"] == "verified":
            assert f["evidence_ids"], "verified finding must carry evidence"
    # conflicts field always present (list, possibly empty)
    assert isinstance(sup.conflicts, list)


def test_single_capability_keeps_legacy_fields_empty():
    task = SoftwareTask(
        task_id="t-single",
        kind=TaskKind.PR_REVIEW,
        goal="review 这个 PR https://github.com/x/y",
        context=TaskContext(repo=_REPO, unified_diff=_DIFF),
    )
    sup = run_supervisor(task, AgentContext(mode="fake"), mode="fake")
    assert sup.orchestration == "single"
    assert sup.result is not None
    # unified-runtime observability is multi-agent only
    assert sup.root_run_id == ""
    assert sup.execution_plan is None
    assert sup.agent_runs == []
    assert sup.aggregate_result is None
