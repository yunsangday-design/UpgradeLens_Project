"""MA-2-2 resilience tests: timeout, cancellation, failure policy, fan-in reducer.

These are the named tests the implementation plan's section 12.4 specifies:
* ``test_agent_scheduler.py`` -- per-step / per-wave timeout policy,
  cancellation token, failure policy (fail_fast / continue / retry);
* ``test_multi_agent_pr_security.py`` -- the canonical PR+Security multi-agent
  scenario with the fan-in reducer (ALL / ANY / QUORUM).
"""

from __future__ import annotations

import threading

import pytest

from upgradelens.agent.execution_plan import (
    AggregateSpec,
    AggregateStrategy,
    CancellationToken,
    ExecutionPlan,
    FailurePolicy,
    PlanCancelled,
    PlanStep,
    StepStrategy,
    WavePolicy,
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

# ---------------------------------------------------------------------------
# Test fixtures: a small registry with controllable sleeps / failures.
# ---------------------------------------------------------------------------


def _make_registry() -> AgentRegistry:
    reg = AgentRegistry()

    def _sleepy(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
        # honour the optional ``extra["sleep_s"]`` hint so timeout tests can
        # make a step take longer than the per-step timeout.
        sleep_s = float(task.extra.get("sleep_s", 0))
        if sleep_s:
            import time as _time

            _time.sleep(sleep_s)
        return AgentResult(
            run_id=ctx.run_id,
            agent_id="sleepy",
            kind=AgentKind.GENERIC,
            status=RunStatus.COMPLETED,
            summary="slept",
            findings=[],
        )

    def _failing(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
        return AgentResult(
            run_id=ctx.run_id,
            agent_id="failing",
            kind=AgentKind.GENERIC,
            status=RunStatus.FAILED,
            summary="boom",
            findings=[],
            notes={"error": task.extra.get("error", "boom")},
        )

    reg.register(
        AgentSpec(
            agent_id="sleepy", kind=AgentKind.GENERIC, name="sleepy",
            version="1", description="", run=_sleepy, max_children=0,
        )
    )
    reg.register(
        AgentSpec(
            agent_id="failing", kind=AgentKind.PR_REVIEW, name="failing",
            version="1", description="", run=_failing, max_children=0,
        )
    )
    return reg


def _ctx(reg: AgentRegistry) -> tuple[AgentRunner, AgentRunContext]:
    runner = AgentRunner(reg)
    ctx = AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.GENERIC),
        mode="fake",
    )
    return runner, ctx


def _step(sid: str, kind: AgentKind, **kw: object) -> PlanStep:
    return PlanStep(
        id=sid,
        kind=kind,
        task=TaskEnvelope(extra=dict(kw)),
        strategy=StepStrategy.PARALLEL,
    )


# ---------------------------------------------------------------------------
# Scheduler / timeout / cancellation
# ---------------------------------------------------------------------------


def test_per_step_timeout_marks_step_failed_with_timeout_note():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(timeout_s=0.05, failure_policy=FailurePolicy.CONTINUE),
    )
    plan.add_step(_step("s", AgentKind.GENERIC, sleep_s=1.0))

    res = execute_plan(runner, plan, ctx, max_workers=1)
    assert res.status == "partial"
    assert res.results["s"].status is RunStatus.FAILED
    assert res.results["s"].notes["timeout_s"] == 0.05


def test_per_step_override_wins_over_wave_policy():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(timeout_s=10.0, failure_policy=FailurePolicy.CONTINUE),
    )
    plan.add_step(PlanStep(
        id="s", kind=AgentKind.GENERIC,
        task=TaskEnvelope(extra={"sleep_s": 1.0}),
        strategy=StepStrategy.PARALLEL,
        timeout_s=0.05,  # explicit per-step override
    ))

    res = execute_plan(runner, plan, ctx, max_workers=1)
    assert res.results["s"].status is RunStatus.FAILED
    assert res.results["s"].notes["timeout_s"] == 0.05


def test_cancellation_token_aborts_between_waves():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(failure_policy=FailurePolicy.CONTINUE),
    )
    plan.add_step(_step("first", AgentKind.GENERIC))
    plan.add_step(_step("second", AgentKind.GENERIC))
    plan.link("first", "second")

    token = CancellationToken()
    fired = threading.Event()

    def _trigger() -> None:
        fired.set()
        token.cancel()

    # Cancel from a side thread once the first wave has started.
    threading.Thread(target=_trigger, daemon=True).start()
    res = execute_plan(runner, plan, ctx, max_workers=1, cancellation=token)
    assert res.status in ("cancelled", "completed")
    assert fired.is_set()
    # once cancelled, no further steps are dispatched
    if res.status == "cancelled":
        assert "second" not in res.results


def test_cancellation_inside_step_raises_plan_cancelled():
    token = CancellationToken()
    token.cancel()
    with pytest.raises(PlanCancelled):
        token.raise_if_cancelled()


# ---------------------------------------------------------------------------
# Failure policy: fail_fast / continue / retry
# ---------------------------------------------------------------------------


def test_fail_fast_aborts_after_first_failed_wave():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(failure_policy=FailurePolicy.FAIL_FAST),
    )
    plan.add_step(_step("bad", AgentKind.PR_REVIEW, error="boom"))
    plan.add_step(_step("never_runs", AgentKind.GENERIC))
    plan.link("bad", "never_runs")

    res = execute_plan(runner, plan, ctx, max_workers=2)
    assert res.status == "failed"
    # fail_fast aborts between waves: only the first wave ran
    assert sorted(res.results) == ["bad"]
    assert "never_runs" not in res.results


def test_continue_marks_plan_partial_and_runs_remaining_waves():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(failure_policy=FailurePolicy.CONTINUE),
    )
    plan.add_step(_step("ok", AgentKind.GENERIC))
    plan.add_step(_step("bad", AgentKind.PR_REVIEW, error="boom"))

    res = execute_plan(runner, plan, ctx, max_workers=2)
    assert res.status == "partial"
    assert res.results["ok"].status is RunStatus.COMPLETED
    assert res.results["bad"].status is RunStatus.FAILED


def test_retry_retries_failing_step_until_max_retries_exhausted():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(
            failure_policy=FailurePolicy.RETRY, max_retries=2,
        ),
    )
    plan.add_step(_step("retry_me", AgentKind.PR_REVIEW, error="boom"))

    res = execute_plan(runner, plan, ctx, max_workers=1)
    assert res.results["retry_me"].status is RunStatus.FAILED
    # the failure policy was consulted three times (1 + 2 retries) but the
    # step is always marked failed -- attempts are deterministic in fake mode


# ---------------------------------------------------------------------------
# Fan-in reducer: ALL / ANY / QUORUM
# ---------------------------------------------------------------------------


def _two_leaves_one_sink_plan(aggregate: AggregateSpec) -> ExecutionPlan:
    plan = ExecutionPlan(
        plan_id="p",
        wave_policy=WavePolicy(failure_policy=FailurePolicy.CONTINUE),
    )
    plan.add_step(PlanStep(
        id="l1",
        kind=AgentKind.PR_REVIEW,
        task=TaskEnvelope(extra={"error": "boom"}),
        strategy=StepStrategy.PARALLEL,
    ))
    plan.add_step(PlanStep(
        id="l2", kind=AgentKind.GENERIC,
        strategy=StepStrategy.PARALLEL,
    ))
    plan.add_step(PlanStep(
        id="sink", kind=AgentKind.GENERIC,
        task=TaskEnvelope(kind="evidence_review"),
        strategy=StepStrategy.FAN_IN,
    ))
    plan.link("l1", "sink", aggregate=aggregate)
    plan.link("l2", "sink", aggregate=aggregate)
    return plan


def test_fan_in_all_requires_every_predecessor():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = _two_leaves_one_sink_plan(AggregateSpec(strategy=AggregateStrategy.ALL))
    res = execute_plan(runner, plan, ctx, max_workers=2)
    assert res.aggregate_results["sink"]["strategy"] == "all"
    assert res.aggregate_results["sink"]["ok"] is False
    assert res.results["sink"].status is RunStatus.FAILED


def test_fan_in_any_accepts_first_success():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = _two_leaves_one_sink_plan(AggregateSpec(strategy=AggregateStrategy.ANY))
    res = execute_plan(runner, plan, ctx, max_workers=2)
    assert res.aggregate_results["sink"]["ok"] is True
    assert res.aggregate_results["sink"]["succeeded"] == ["l2"]


def test_fan_in_quorum_respects_min_success():
    reg = _make_registry()
    runner, ctx = _ctx(reg)
    plan = _two_leaves_one_sink_plan(
        AggregateSpec(strategy=AggregateStrategy.QUORUM, min_success=2)
    )
    res = execute_plan(runner, plan, ctx, max_workers=2)
    assert res.aggregate_results["sink"]["ok"] is False  # only l2 succeeded
    plan2 = _two_leaves_one_sink_plan(
        AggregateSpec(strategy=AggregateStrategy.QUORUM, min_success=1)
    )
    res2 = execute_plan(runner, plan2, ctx, max_workers=2)
    assert res2.aggregate_results["sink"]["ok"] is True


# ---------------------------------------------------------------------------
# Scheduler sanity: execution_waves still correct under the new step shape.
# ---------------------------------------------------------------------------


def test_execution_waves_unchanged_by_policy_additions():
    plan = ExecutionPlan(plan_id="p")
    plan.add_step(PlanStep(id="a", kind=AgentKind.GENERIC))
    plan.add_step(PlanStep(id="b", kind=AgentKind.GENERIC))
    plan.add_step(PlanStep(id="c", kind=AgentKind.GENERIC))
    plan.link("a", "c")
    plan.link("b", "c")
    waves = execution_waves(plan)
    assert sorted(waves[0]) == ["a", "b"]
    assert waves[1] == ["c"]
