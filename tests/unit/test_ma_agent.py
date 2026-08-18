"""Tests for AgentSpec/Registry/Runner/Checkpoint (MA-1B-1 / MA-1B-2 / MA-1B-3)."""

from __future__ import annotations

from upgradelens.agent.budget import BudgetLedger, BudgetSpec
from upgradelens.agent.checkpoint import (
    MemoryCheckpointStore,
    SQLiteCheckpointStore,
    run_with_checkpoint,
)
from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentRunContext,
    LifecycleEvent,
    RunStatus,
    TaskEnvelope,
    new_run_id,
)
from upgradelens.agent.spec import AgentFn, AgentRegistry, AgentSpec, default_registry

# -- a fake professional agent ------------------------------------------------


def _fake_agent(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
    return AgentResult(
        run_id=ctx.run_id,
        parent_run_id=ctx.parent_run_id,
        agent_id=ctx.agent.agent_id,
        kind=ctx.agent.kind,
        status=RunStatus.COMPLETED,
        summary=f"did {task.kind}",
        findings=[],
    )


def _make_registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(
        AgentSpec(
            agent_id="fake_pr",
            kind=AgentKind.PR_REVIEW,
            name="Fake PR",
            run=_fake_agent,
        )
    )
    return reg


def _ctx(mode: str = "fake") -> AgentRunContext:
    return AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.SUPERVISOR),
        mode=mode,
        budget=BudgetLedger(spec=BudgetSpec(max_total_tokens=1000)),
    )


def test_default_registry_has_five_professional_agents() -> None:
    reg = default_registry()
    kinds = {s.kind for s in reg.list_specs()}
    assert AgentKind.DEPENDENCY_UPGRADE in kinds
    assert AgentKind.PR_REVIEW in kinds
    assert AgentKind.ISSUE_REPAIR in kinds
    assert AgentKind.SECURITY_REVIEW in kinds
    assert AgentKind.BREAKING_CHANGE in kinds
    # every spec is runnable
    for spec in reg.list_specs():
        assert spec.run is not None


def test_registry_rejects_spec_without_run() -> None:
    reg = AgentRegistry()
    try:
        reg.register(AgentSpec(agent_id="x", kind=AgentKind.GENERIC, name="x"))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_registry_resolve_missing_raises() -> None:
    reg = AgentRegistry()
    try:
        reg.resolve(AgentKind.GENERIC)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_runner_emits_lifecycle_and_runs_agent() -> None:
    events: list[LifecycleEvent] = []
    runner = AgentRunner(_make_registry(), on_event=events.append)
    ctx = _ctx()
    result = runner.run(
        AgentKind.PR_REVIEW, ctx, TaskEnvelope(kind="pr_review", repo="/tmp/x")
    )
    assert result.status is RunStatus.COMPLETED
    assert result.summary == "did pr_review"
    kinds = [e.event for e in events]
    assert "start" in kinds and "finish" in kinds


def test_runner_captures_agent_failure() -> None:
    def _boom(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
        raise RuntimeError("kaboom")

    reg = AgentRegistry()
    reg.register(
        AgentSpec(agent_id="boom", kind=AgentKind.ISSUE_REPAIR, name="boom", run=_boom)
    )
    runner = AgentRunner(reg)
    result = runner.run(AgentKind.ISSUE_REPAIR, _ctx(), TaskEnvelope(kind="issue_repair"))
    assert result.status is RunStatus.FAILED
    assert "kaboom" in result.summary


def test_runner_child_threads_parent_run_id() -> None:
    runner = AgentRunner(_make_registry())
    parent = _ctx()
    child = runner.run_child(parent, AgentKind.PR_REVIEW, TaskEnvelope(kind="pr_review"))
    assert child.parent_run_id == parent.run_id
    assert child.run_id != parent.run_id


def test_runner_records_cost_into_shared_ledger() -> None:
    ledger = BudgetLedger(spec=BudgetSpec(max_total_tokens=1000))
    ctx = AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.SUPERVISOR),
        budget=ledger,
    )

    def _spend(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
        res = _fake_agent(ctx, task)
        res.cost = res.cost.model_copy(update={"input_tokens": 200})
        return res

    reg = AgentRegistry()
    reg.register(
        AgentSpec(agent_id="spend", kind=AgentKind.PR_REVIEW, name="spend", run=_spend)
    )
    runner = AgentRunner(reg)
    runner.run(AgentKind.PR_REVIEW, ctx, TaskEnvelope(kind="pr_review"))
    assert ledger.total.input_tokens == 200


def test_memory_checkpoint_resumes_completed_run() -> None:
    store = MemoryCheckpointStore()
    runner = AgentRunner(_make_registry())
    ctx = _ctx()
    first = run_with_checkpoint(
        runner, AgentKind.PR_REVIEW, ctx, TaskEnvelope(kind="pr_review"), store
    )
    assert first.status is RunStatus.COMPLETED
    assert store.completed(ctx.run_id)
    # second dispatch with same run_id resumes from checkpoint (no recompute)
    second = run_with_checkpoint(
        runner, AgentKind.PR_REVIEW, ctx, TaskEnvelope(kind="pr_review"), store
    )
    assert second.notes.get("resumed_from_checkpoint") is True
    assert second.summary == first.summary


def test_sqlite_checkpoint_store_roundtrip() -> None:
    import sqlite3
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "cp.db"
    conn = sqlite3.connect(str(tmp))
    store = SQLiteCheckpointStore(conn)
    runner = AgentRunner(_make_registry())
    ctx = _ctx()
    run_with_checkpoint(runner, AgentKind.PR_REVIEW, ctx, TaskEnvelope(kind="pr_review"), store)
    assert store.completed(ctx.run_id)
    loaded = store.latest(ctx.run_id)
    assert loaded is not None
    assert loaded.state.get("status") == RunStatus.COMPLETED.value
    conn.close()


def test_agent_fn_protocol_is_callable() -> None:
    # mypy-level: AgentFn is a Protocol matching _fake_agent
    fn: AgentFn = _fake_agent
    assert callable(fn)
