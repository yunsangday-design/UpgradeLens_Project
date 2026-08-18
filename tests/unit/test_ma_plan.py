"""Tests for ExecutionPlan DAG + scheduler + ResultAggregator (MA-2)."""

from __future__ import annotations

from upgradelens.agent.aggregator import ResultAggregator
from upgradelens.agent.budget import BudgetLedger, BudgetSpec
from upgradelens.agent.execution_plan import (
    ExecutionPlan,
    PlanStep,
    execute_plan,
    execution_waves,
)
from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentRunContext,
    RunStatus,
    TaskEnvelope,
    new_run_id,
)
from upgradelens.agent.spec import AgentRegistry, AgentSpec
from upgradelens.core.finding import Finding, Severity


def _fake_agent(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
    return AgentResult(
        run_id=ctx.run_id,
        parent_run_id=ctx.parent_run_id,
        agent_id=ctx.agent.agent_id,
        kind=ctx.agent.kind,
        status=RunStatus.COMPLETED,
        summary=f"ran {task.kind}",
        findings=[],
    )


def _registry() -> AgentRegistry:
    reg = AgentRegistry()
    for kind in (AgentKind.PR_REVIEW, AgentKind.SECURITY_REVIEW, AgentKind.ISSUE_REPAIR):
        reg.register(AgentSpec(agent_id=kind.value, kind=kind, name=kind.value, run=_fake_agent))
    return reg


def _ctx() -> AgentRunContext:
    return AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.SUPERVISOR),
        budget=BudgetLedger(spec=BudgetSpec(max_total_tokens=10_000)),
    )


def test_execution_waves_sequential() -> None:
    plan = ExecutionPlan(plan_id="p", mode="fake")
    plan.add_step(PlanStep(id="a", kind=AgentKind.PR_REVIEW))
    plan.add_step(PlanStep(id="b", kind=AgentKind.SECURITY_REVIEW, depends_on=["a"]))
    plan.add_step(PlanStep(id="c", kind=AgentKind.ISSUE_REPAIR, depends_on=["b"]))
    waves = execution_waves(plan)
    assert waves == [["a"], ["b"], ["c"]]


def test_execution_waves_fan_out_and_fan_in() -> None:
    plan = ExecutionPlan(plan_id="p", mode="fake")
    plan.add_step(PlanStep(id="root", kind=AgentKind.PR_REVIEW))
    plan.add_step(PlanStep(id="x", kind=AgentKind.SECURITY_REVIEW, depends_on=["root"]))
    plan.add_step(PlanStep(id="y", kind=AgentKind.ISSUE_REPAIR, depends_on=["root"]))
    plan.add_step(PlanStep(id="sink", kind=AgentKind.PR_REVIEW, depends_on=["x", "y"]))
    waves = execution_waves(plan)
    assert waves[0] == ["root"]
    assert set(waves[1]) == {"x", "y"}  # fan-out parallel wave
    assert waves[2] == ["sink"]  # fan-in


def test_plan_detects_cycle() -> None:
    plan = ExecutionPlan(plan_id="p", mode="fake")
    plan.add_step(PlanStep(id="a", kind=AgentKind.PR_REVIEW))
    plan.add_step(PlanStep(id="b", kind=AgentKind.SECURITY_REVIEW))
    plan.link("a", "b")
    plan.link("b", "a")
    assert not plan.is_acyclic()


def test_execute_plan_runs_all_steps_under_one_budget() -> None:
    plan = ExecutionPlan(plan_id="p", mode="fake")
    plan.add_step(PlanStep(id="a", kind=AgentKind.PR_REVIEW))
    plan.add_step(PlanStep(id="b", kind=AgentKind.SECURITY_REVIEW, depends_on=["a"]))
    runner = AgentRunner(_registry())
    result = execute_plan(runner, plan, _ctx())
    assert result.status == "completed"
    assert set(result.results) == {"a", "b"}


def test_execute_plan_parallel_fan_out() -> None:
    plan = ExecutionPlan(plan_id="p", mode="fake")
    plan.add_step(PlanStep(id="root", kind=AgentKind.PR_REVIEW))
    plan.add_step(PlanStep(id="x", kind=AgentKind.SECURITY_REVIEW, depends_on=["root"]))
    plan.add_step(PlanStep(id="y", kind=AgentKind.ISSUE_REPAIR, depends_on=["root"]))
    runner = AgentRunner(_registry())
    result = execute_plan(runner, plan, _ctx(), max_workers=2)
    assert result.status == "completed"
    assert set(result.results) == {"root", "x", "y"}


def _finding(fid: str, sev: Severity) -> Finding:
    return Finding(
        finding_id=fid,
        category="c",
        severity=sev,
        confidence=0.9,
        summary=f"f {fid}",
        evidence_ids=["e1"],
    )


def test_aggregator_merges_and_dedups() -> None:
    agg = ResultAggregator()
    ctx = _ctx()
    r1 = AgentResult(
        run_id=new_run_id(),
        agent_id="a",
        kind=AgentKind.PR_REVIEW,
        findings=[_finding("f1", Severity.LOW)],
    )
    r2 = AgentResult(
        run_id=new_run_id(),
        agent_id="b",
        kind=AgentKind.SECURITY_REVIEW,
        findings=[_finding("f2", Severity.HIGH)],
    )
    merged = agg.aggregate(ctx, [r1, r2])
    assert {f.finding_id for f in merged.findings} == {"f1", "f2"}


def test_aggregator_surfaces_conflict_on_severity_mismatch() -> None:
    agg = ResultAggregator()
    ctx = _ctx()
    low = AgentResult(
        run_id=new_run_id(),
        agent_id="a",
        kind=AgentKind.PR_REVIEW,
        findings=[_finding("same", Severity.LOW)],
    )
    high = AgentResult(
        run_id=new_run_id(),
        agent_id="b",
        kind=AgentKind.SECURITY_REVIEW,
        findings=[_finding("same", Severity.HIGH)],
    )
    merged = agg.aggregate(ctx, [low, high])
    assert len(merged.findings) == 1
    # higher severity wins
    assert merged.findings[0].severity is Severity.HIGH
    assert merged.notes["conflicts"]
    assert merged.notes["conflicts"][0]["finding_id"] == "same"


def test_aggregator_marks_failed_when_child_fails() -> None:
    agg = ResultAggregator()
    ctx = _ctx()
    ok = AgentResult(run_id=new_run_id(), agent_id="a", kind=AgentKind.PR_REVIEW)
    bad = AgentResult(
        run_id=new_run_id(), agent_id="b", kind=AgentKind.SECURITY_REVIEW, status=RunStatus.FAILED
    )
    merged = agg.aggregate(ctx, [ok, bad])
    assert merged.status is RunStatus.FAILED
