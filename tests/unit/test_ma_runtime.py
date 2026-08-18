"""Tests for the unified multi-agent runtime contracts (MA-1A-1 / MA-1A-2)."""

from __future__ import annotations

from upgradelens.agent.budget import (
    BudgetExhausted,
    BudgetLedger,
    BudgetPolicy,
    BudgetSpec,
    default_budget_spec,
)
from upgradelens.agent.runtime import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentRunContext,
    Checkpoint,
    CostUsage,
    LifecycleEvent,
    RunId,
    RunStatus,
    TraceNode,
    new_run_id,
)


def test_new_run_id_is_short_and_unique() -> None:
    a = new_run_id()
    b = new_run_id()
    assert isinstance(a, str)
    assert len(a) == 8
    assert a != b
    # NewType preserves str behaviour for concatenation in fixtures if needed.
    assert RunId(a) == a


def test_cost_usage_merge_sums_and_is_immutable() -> None:
    c1 = CostUsage(model="m", input_tokens=10, output_tokens=5, tool_calls=2, cost_usd=0.1)
    c2 = CostUsage(model="m", input_tokens=20, output_tokens=3, tool_calls=1, cost_usd=0.2)
    merged = c1.merge(c2)
    assert merged.input_tokens == 30
    assert merged.output_tokens == 8
    assert merged.tool_calls == 3
    # float-robust comparison
    assert abs(merged.cost_usd - 0.3) < 1e-9
    assert merged.total == 38
    # original untouched
    assert c1.input_tokens == 10


def test_cost_usage_from_ledger_entry_lossless() -> None:
    entry = {
        "input_tokens": 7,
        "output_tokens": 8,
        "cache_read_tokens": 3,
        "total_tokens": 18,
        "model": "gpt-x",
        "tool_calls": 4,
        "latency_ms": 120.0,
        "cost_usd": 0.05,
    }
    cost = CostUsage.from_ledger_entry(entry)
    assert cost.input_tokens == 7
    assert cost.total_tokens == 18
    assert cost.model == "gpt-x"
    assert cost.latency_ms == 120.0


def test_agent_identity_create() -> None:
    ident = AgentIdentity.create(AgentKind.DEPENDENCY_UPGRADE, version="2.3.1")
    assert ident.kind is AgentKind.DEPENDENCY_UPGRADE
    assert ident.agent_id == "dependency_upgrade"
    assert ident.version == "2.3.1"


def test_run_context_child_inherits_and_threads_parent() -> None:
    parent = AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.SUPERVISOR),
        mode="live",
        locale="en",
    )
    child_ctx = parent.child(AgentIdentity.create(AgentKind.PR_REVIEW))
    assert child_ctx.parent_run_id == parent.run_id
    assert child_ctx.mode == "live"
    assert child_ctx.locale == "en"
    assert child_ctx.run_id != parent.run_id


def test_run_context_permissions_default_deny() -> None:
    ctx = AgentRunContext(run_id=new_run_id(), agent=AgentIdentity.create(AgentKind.GENERIC))
    assert ctx.has_permission("read_repo")
    assert not ctx.has_permission("write_repo")


def test_lifecycle_event_and_checkpoint_and_trace_node() -> None:
    rid = new_run_id()
    evt = LifecycleEvent(run_id=rid, event="start")
    assert evt.event == "start"
    assert evt.data == {}

    cp = Checkpoint(run_id=rid, step="s1", state={"x": 1}, state_hash="h")
    assert cp.is_resumable()

    node = TraceNode(id="n1", agent_id="pr_review", kind="capability", label="review")
    assert node.status is RunStatus.PENDING
    assert node.cost.total == 0


def test_agent_result_bridge_to_capability_shape() -> None:
    rid = new_run_id()
    res = AgentResult(run_id=rid, agent_id="x", kind=AgentKind.SECURITY_REVIEW, summary="ok")
    payload = res.to_capability_result()
    assert payload["capability"] == "security_review"
    assert payload["summary"] == "ok"
    assert payload["findings"] == []


def test_budget_ledger_records_and_reports() -> None:
    ledger = BudgetLedger(spec=BudgetSpec(max_total_tokens=100, max_tool_calls=5))
    ledger.record(CostUsage(input_tokens=40, output_tokens=10))
    ledger.record(CostUsage(input_tokens=30, tool_calls=2))
    assert ledger.total.input_tokens == 70
    assert ledger.remaining_tokens() == 20
    assert not ledger.is_exhausted()


def test_budget_ledger_warn_policy_does_not_raise() -> None:
    ledger = BudgetLedger(
        spec=BudgetSpec(max_total_tokens=50), policy=BudgetPolicy.WARN
    )
    ledger.record(CostUsage(input_tokens=80))
    assert ledger.is_exhausted()
    assert "max_total_tokens" in ledger.breached_dimensions()


def test_budget_ledger_fail_policy_raises() -> None:
    ledger = BudgetLedger(
        spec=BudgetSpec(max_total_tokens=50), policy=BudgetPolicy.FAIL
    )
    try:
        ledger.record(CostUsage(input_tokens=80))
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted as exc:  # noqa: PT012
        assert "max_total_tokens" in str(exc)


def test_budget_ledger_from_legacy_entry() -> None:
    ledger = BudgetLedger(spec=default_budget_spec(max_total_tokens=1000))
    ledger.record_legacy_entry(
        {"input_tokens": 10, "output_tokens": 5, "tool_calls": 1, "cost_usd": 0.01, "model": "m"}
    )
    assert ledger.total.input_tokens == 10
    assert ledger.total.cost_usd == 0.01
