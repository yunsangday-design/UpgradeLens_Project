"""Execution plan DAG, scheduler and supervised execution (MA-2-1 / MA-2-2).

A :class:`ExecutionPlan` is a capability-agnostic DAG of :class:`PlanStep` nodes.
Each step names an :class:`AgentKind` and a :class:`TaskEnvelope`; edges express
dependencies and the *strategy* a supervisor uses to expand/collect them:

* ``sequential`` -- one step after another (the default single-capability path);
* ``parallel`` -- independent steps in the same wave (bounded fan-out);
* ``fan_out`` -- a parent spawns several child agents that all read its output;
* ``fan_in`` -- several steps converge into one aggregator step.

::func:`execution_waves` topologically layers the DAG into waves that can run in
parallel; :func:`execute_plan` drives those waves through an
:class:`~upgradelens.agent.runner.AgentRunner` under one shared budget, so the
multi-agent supervisor reuses the single-capability engines unchanged.

MA-2-2 resilience: every wave is governed by a :class:`WavePolicy` -- per-step
``timeout``, a :class:`CancellationToken` to abort early, and a failure policy
(``fail_fast`` / ``continue`` / ``retry``). The fan-in reducer collects step
results through an :class:`AggregateStrategy` (``all`` / ``any`` / ``quorum``).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.agent.checkpoint import CheckpointStore
from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
    RunId,
    RunStatus,
    TaskEnvelope,
)


class StepStrategy(StrEnum):
    """How a step relates to its predecessors."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"


class FailurePolicy(StrEnum):
    """How a wave reacts to a failing step (MA-2-2)."""

    FAIL_FAST = "fail_fast"  # abort the plan on the first failure
    CONTINUE = "continue"  # run remaining steps, mark plan as failed
    RETRY = "retry"  # retry the failing step once before propagating


class AggregateStrategy(StrEnum):
    """How a fan-in step collects its inputs (MA-2-2).

    ``all`` -- every predecessor must succeed (default);
    ``any`` -- at least one predecessor must succeed;
    ``quorum`` -- ``min_success`` predecessors must succeed.
    """

    ALL = "all"
    ANY = "any"
    QUORUM = "quorum"


@dataclass
class WavePolicy:
    """Per-wave execution policy (MA-2-2)."""

    timeout_s: float | None = None  # None == no per-step timeout
    failure_policy: FailurePolicy = FailurePolicy.CONTINUE
    max_retries: int = 0  # used when failure_policy == RETRY


@dataclass
class AggregateSpec:
    """Fan-in collection rules (MA-2-2).

    ``min_success`` is consulted when ``strategy`` is ``QUORUM``.
    """

    strategy: AggregateStrategy = AggregateStrategy.ALL
    min_success: int = 1


class CancellationToken:
    """Thread-safe one-shot cancellation flag (MA-2-2)."""

    def __init__(self) -> None:
        self._evt = threading.Event()

    def cancel(self) -> None:
        self._evt.set()

    @property
    def cancelled(self) -> bool:
        return self._evt.is_set()

    def raise_if_cancelled(self) -> None:
        if self._evt.is_set():
            raise PlanCancelled("plan cancelled by token")


class PlanCancelled(RuntimeError):
    """Raised by :func:`execute_plan` when the cancellation token fires."""


class PlanStep(BaseModel):
    """One node in an execution DAG."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: AgentKind
    task: TaskEnvelope = Field(default_factory=TaskEnvelope)
    depends_on: list[str] = Field(default_factory=list)
    strategy: StepStrategy = StepStrategy.SEQUENTIAL
    max_children: int = 0
    # Optional per-step overrides (MA-2-2). When ``None``, the wave's policy wins.
    timeout_s: float | None = None
    failure_policy: FailurePolicy | None = None
    max_retries: int | None = None


class PlanEdge(BaseModel):
    """A dependency edge between two steps, tagged with its strategy."""

    model_config = ConfigDict(extra="forbid")

    from_step: str
    to_step: str
    strategy: StepStrategy = StepStrategy.SEQUENTIAL
    aggregate: AggregateSpec = Field(default_factory=AggregateSpec)


class ExecutionPlan(BaseModel):
    """A capability-agnostic DAG of agent steps."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    steps: dict[str, PlanStep] = Field(default_factory=dict)
    edges: list[PlanEdge] = Field(default_factory=list)
    mode: str = "fake"
    # MA-2-2: per-wave policy; the same policy governs every wave unless a
    # step declares its own override.
    wave_policy: WavePolicy = Field(default_factory=WavePolicy)

    # -- construction ----------------------------------------------------- #

    def add_step(self, step: PlanStep) -> None:
        if step.id in self.steps:
            raise ValueError(f"duplicate step id {step.id!r}")
        self.steps[step.id] = step

    def add_edge(self, edge: PlanEdge) -> None:
        if edge.from_step not in self.steps or edge.to_step not in self.steps:
            raise ValueError("edge references unknown step")
        self.edges.append(edge)
        to = self.steps[edge.to_step]
        if edge.from_step not in to.depends_on:
            to.depends_on.append(edge.from_step)

    def link(
        self,
        from_step: str,
        to_step: str,
        *,
        strategy: StepStrategy = StepStrategy.SEQUENTIAL,
        aggregate: AggregateSpec | None = None,
    ) -> None:
        self.add_edge(
            PlanEdge(
                from_step=from_step,
                to_step=to_step,
                strategy=strategy,
                aggregate=aggregate or AggregateSpec(),
            )
        )

    # -- queries ---------------------------------------------------------- #

    def roots(self) -> list[str]:
        """Steps with no predecessors."""
        has_parent = {e.to_step for e in self.edges}
        return [sid for sid in self.steps if sid not in has_parent]

    def successors(self, step_id: str) -> list[str]:
        return [e.to_step for e in self.edges if e.from_step == step_id]

    def aggregate_for(self, step_id: str) -> AggregateSpec:
        for e in self.edges:
            if e.to_step == step_id:
                return e.aggregate
        return AggregateSpec()

    def is_acyclic(self) -> bool:
        """Detect cycles via DFS colouring."""
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {sid: WHITE for sid in self.steps}

        def visit(nid: str) -> bool:
            color[nid] = GREY
            for nxt in self.successors(nid):
                if color.get(nxt) == GREY:
                    return False
                if color.get(nxt) == WHITE and not visit(nxt):
                    return False
            color[nid] = BLACK
            return True

        return all(visit(sid) for sid in self.steps if color[sid] == WHITE)


def execution_waves(plan: ExecutionPlan) -> list[list[str]]:
    """Layer the DAG into waves; each wave's steps may run in parallel.

    Raises ``ValueError`` on a cyclic plan. Steps in the same wave share no
    unsatisfied dependency, so a wave is exactly the unit of fan-out / parallel
    execution the supervisor dispatches together.
    """
    if not plan.is_acyclic():
        raise ValueError("execution plan contains a cycle")

    completed: set[str] = set()
    waves: list[list[str]] = []
    remaining = set(plan.steps)

    while remaining:
        ready = [
            sid
            for sid in remaining
            if all(dep in completed for dep in plan.steps[sid].depends_on)
        ]
        if not ready:
            # defensive: should not happen for acyclic plans
            raise ValueError("execution plan has an unsatisfied dependency")
        waves.append(sorted(ready))
        completed.update(ready)
        remaining -= set(ready)

    return waves


# ---------------------------------------------------------------------------
# Supervised execution
# ---------------------------------------------------------------------------


@dataclass
class PlanExecutionResult:
    """Outcome of running a whole plan."""

    plan_id: str
    results: dict[str, AgentResult]
    waves: list[list[str]]
    status: str = "completed"  # completed | failed | partial | cancelled
    # steps whose results were restored from a checkpoint instead of re-run
    resumed_steps: list[str] = field(default_factory=list)
    # MA-2-2: how the fan-in reducer evaluated its inputs (per fan-in step)
    aggregate_results: dict[str, dict[str, Any]] = field(default_factory=dict)


def execute_plan(
    runner: AgentRunner,
    plan: ExecutionPlan,
    ctx: AgentRunContext,
    *,
    max_workers: int = 1,
    checkpoint_store: CheckpointStore | None = None,
    cancellation: CancellationToken | None = None,
) -> PlanExecutionResult:
    """Drive the plan's waves through ``runner`` under one shared budget.

    Within a wave, steps run concurrently (bounded by ``max_workers``) as *child*
    runs of ``ctx` (so every leaf carries ``parent_run_id`` and the trace tree
    stays connected). Cost is recorded once, by the runner, into the shared
    (thread-safe) ledger on the context.

    Resilience (MA-2-2):

    * ``plan.wave_policy.timeout_s`` bounds a single step's execution; a timeout
      turns into a ``FAILED`` result with ``notes={"timeout_s": ...}`` (the
      underlying thread is daemonised and abandoned).
    * ``cancellation.cancel()`` aborts the plan between waves; in-flight steps
      are not interrupted mid-call but the plan raises :class:`PlanCancelled`
      after the current wave completes.
    * ``plan.wave_policy.failure_policy`` controls whether a failed step aborts
      the plan (``FAIL_FAST``), is logged and the wave continues
      (``CONTINUE``, the default), or is retried up to ``max_retries``
      (``RETRY``).
    * ``PlanEdge.aggregate`` decides what counts as success at each fan-in sink:
      ``ALL`` (default), ``ANY``, or ``QUORUM`` with ``min_success``.

    With a ``checkpoint_store``, completed steps are checkpointed under
    ``(ctx.run_id, step_id)`` and a re-dispatch of the same run id resumes them
    losslessly instead of re-executing -- the crash-recovery guarantee.
    """
    waves = execution_waves(plan)
    results: dict[str, AgentResult] = {}
    resumed: list[str] = []
    aggregate_results: dict[str, dict[str, Any]] = {}
    any_failed = False
    status = "completed"
    token = cancellation or CancellationToken()

    # cumulative "have run" map for fan-in evaluation across waves
    cumulative: dict[str, AgentResult] = {}


    for wave_idx, wave in enumerate(waves):
        if token.cancelled:
            status = "cancelled"
            break
        wave_results, wave_failed, agg, resumed_in_wave = _dispatch_wave(
            runner, plan, ctx, wave, checkpoint_store, token, max_workers, cumulative
        )
        cumulative.update(wave_results)
        for sid, res in wave_results.items():
            results[sid] = res
            if res.status.value != "completed":
                any_failed = True
        resumed.extend(resumed_in_wave)
        aggregate_results.update(agg)
        if wave_failed and plan.wave_policy.failure_policy is FailurePolicy.FAIL_FAST:
            status = "failed"
            break
        if wave_failed:
            status = "partial"
        # Checkpoint the newly completed (or failed) steps
        if checkpoint_store is not None:
            from upgradelens.agent.runtime import Checkpoint

            for sid, res in wave_results.items():
                if res.status is RunStatus.COMPLETED:
                    dump = res.model_dump(mode="json")
                    checkpoint_store.save(
                        Checkpoint(
                            run_id=ctx.run_id,
                            step=sid,
                            state={"status": RunStatus.COMPLETED.value, "result": dump},
                            state_hash=_hash_result(res),
                        )
                    )
        _ = wave_idx  # noqa: F841 -- wave index is reserved for future trace hooks

    if any_failed and status == "completed":
        status = "failed"

    return PlanExecutionResult(
        plan_id=plan.plan_id,
        results=results,
        waves=waves,
        status=status,
        resumed_steps=resumed,
        aggregate_results=aggregate_results,
    )


def _dispatch_wave(
    runner: AgentRunner,
    plan: ExecutionPlan,
    ctx: AgentRunContext,
    wave: list[str],
    checkpoint_store: CheckpointStore | None,
    token: CancellationToken,
    max_workers: int = 1,
    prior_results: dict[str, AgentResult] | None = None,
) -> tuple[dict[str, AgentResult], bool, dict[str, dict[str, Any]], list[str]]:
    """Run one wave; returns (results, wave_failed, aggregate_outcomes, resumed_in_wave).

    ``prior_results`` carries results from earlier waves so fan-in sinks can
    evaluate their aggregate strategy against predecessors that ran in a
    previous wave.
    """
    wave_results: dict[str, AgentResult] = {}
    aggregate_outcomes: dict[str, dict[str, Any]] = {}
    resumed_in_wave: list[str] = []
    pending = list(wave)

    # 1) restore completed steps from a checkpoint if present
    if checkpoint_store is not None:
        still_pending: list[str] = []
        for sid in pending:
            cp = checkpoint_store.load(ctx.run_id, sid)
            if cp is None or cp.state.get("status") != RunStatus.COMPLETED.value:
                still_pending.append(sid)
                continue
            wave_results[sid] = AgentResult.model_validate(cp.state["result"])
            resumed_in_wave.append(sid)
            # resumed costs were spent in the crashed attempt; keep the ledger honest
            if ctx.budget is not None and hasattr(ctx.budget, "record"):
                ctx.budget.record(wave_results[sid].cost)
        pending = still_pending

    # 2) run the rest (with per-step timeouts / retries)
    wave_failed = False
    if max_workers <= 1 or len(pending) <= 1:
        for sid in pending:
            res = _run_step_with_policy(runner, plan, ctx, sid, token)
            wave_results[sid] = res
            if res.status.value != "completed":
                wave_failed = True
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_step_with_policy, runner, plan, ctx, sid, token): sid
                for sid in pending
            }
            for fut in futures:
                sid = futures[fut]
                wave_results[sid] = fut.result()
                if wave_results[sid].status.value != "completed":
                    wave_failed = True

    # 3) evaluate each fan-in step's aggregate strategy (MA-2-2)
    aggregate_pool: dict[str, AgentResult] = dict(prior_results or {})
    aggregate_pool.update(wave_results)
    for sid in wave:
        step = plan.steps[sid]
        if step.strategy is not StepStrategy.FAN_IN:
            continue
        agg = plan.aggregate_for(sid)
        predecessors = [
            e.from_step for e in plan.edges if e.to_step == sid
        ]
        if not predecessors:
            continue
        successes = [
            p for p in predecessors
            if aggregate_pool.get(p, _missing()).status is RunStatus.COMPLETED
        ]
        outcome = _evaluate_aggregate(agg, predecessors, successes)
        aggregate_outcomes[sid] = outcome
        # An aggregate that *fails* its reducer downgrades the sink.
        if not outcome["ok"]:
            wave_results[sid] = _aggregate_failure(step, outcome)
            wave_failed = True

    return wave_results, wave_failed, aggregate_outcomes, resumed_in_wave


def _missing() -> AgentResult:
    """Sentinel used when a predecessor is absent from the wave (shouldn't happen)."""
    return AgentResult(
        run_id=RunId("missing"),
        agent_id="missing",
        kind=AgentKind.GENERIC,
        status=RunStatus.FAILED,
        summary="missing predecessor",
        findings=[],
    )


def _evaluate_aggregate(
    agg: AggregateSpec, predecessors: list[str], successes: list[str]
) -> dict[str, Any]:
    """Apply an :class:`AggregateSpec` to a set of predecessor outcomes."""
    ok: bool
    if agg.strategy is AggregateStrategy.ALL:
        ok = len(successes) == len(predecessors)
    elif agg.strategy is AggregateStrategy.ANY:
        ok = len(successes) >= 1
    else:  # QUORUM
        ok = len(successes) >= max(1, agg.min_success)
    return {
        "strategy": agg.strategy.value,
        "min_success": agg.min_success,
        "succeeded": sorted(successes),
        "failed": sorted(p for p in predecessors if p not in successes),
        "ok": ok,
    }


def _aggregate_failure(step: PlanStep, outcome: dict[str, Any]) -> AgentResult:
    return AgentResult(
        run_id=RunId("aggregate"),
        agent_id=step.id,
        kind=step.kind,
        status=RunStatus.FAILED,
        summary=(
            f"fan-in aggregate ({outcome['strategy']}) failed: "
            f"ok={outcome['ok']}, succeeded={outcome['succeeded']}"
        ),
        findings=[],
        notes={"aggregate_outcome": outcome},
    )


def _run_step_with_policy(
    runner: AgentRunner,
    plan: ExecutionPlan,
    ctx: AgentRunContext,
    step_id: str,
    token: CancellationToken,
) -> AgentResult:
    """Run a single step under its timeout / retry / cancellation policy."""
    step = plan.steps[step_id]
    timeout_s = step.timeout_s if step.timeout_s is not None else plan.wave_policy.timeout_s
    failure_policy = step.failure_policy or plan.wave_policy.failure_policy
    max_retries = (
        step.max_retries if step.max_retries is not None else plan.wave_policy.max_retries
    )

    attempts = max_retries + 1 if failure_policy is FailurePolicy.RETRY else 1
    last_result: AgentResult | None = None
    for attempt in range(attempts):
        token.raise_if_cancelled()
        last_result = _invoke_with_timeout(runner, ctx, step, timeout_s, token)
        if last_result.status is RunStatus.COMPLETED:
            return last_result
        if failure_policy is FailurePolicy.RETRY and attempt + 1 < attempts:
            continue
        break
    return last_result  # type: ignore[return-value]


def _invoke_with_timeout(
    runner: AgentRunner,
    ctx: AgentRunContext,
    step: PlanStep,
    timeout_s: float | None,
    token: CancellationToken,
) -> AgentResult:
    """Execute one step; honour ``timeout_s`` by running in a daemon thread."""
    target: Callable[[], AgentResult] = lambda: runner.run_child(ctx, step.kind, step.task)  # noqa: E731
    if timeout_s is None or timeout_s <= 0:
        return target()
    holder: dict[str, AgentResult | BaseException] = {}

    def _runner() -> None:
        try:
            holder["result"] = target()
        except BaseException as exc:  # noqa: BLE001
            holder["result"] = exc

    thread = threading.Thread(target=_runner, daemon=True, name=f"step-{step.id}")
    thread.start()
    thread.join(timeout=timeout_s)
    if thread.is_alive():
        # abandon the daemon thread; return a timeout-tagged failure

        return AgentResult(
            run_id=ctx.run_id,
            agent_id=step.id,
            kind=step.kind,
            status=RunStatus.FAILED,
            summary=f"{step.id} timed out after {timeout_s:.2f}s",
            findings=[],
            notes={"timeout_s": timeout_s, "cancelled": token.cancelled},
        )
    result = holder.get("result")
    if isinstance(result, BaseException):
        raise result
    return result  # type: ignore[return-value]


def _hash_result(result: AgentResult) -> str:
    blob = f"{result.status.value}|{result.agent_id}|{result.finding_count}".encode()
    return hashlib.sha1(blob).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Canonical fan-out / fan-in template (shared by supervisor and MA-4 decomposer)
# ---------------------------------------------------------------------------


def fanout_fanin_plan(
    leaves: list[PlanStep],
    plan_id: str,
    *,
    mode: str = "fake",
    sink_id: str = "evidence_review",
    aggregate: AggregateSpec | None = None,
) -> ExecutionPlan:
    """The canonical multi-agent template: independent parallel leaves
    converging on one evidence-review fan-in step.

    Both the Supervisor (classified multi-capability requests) and the
    constrained :class:`~upgradelens.agent.decomposer.DynamicDecomposer`
    (meta goals like ``full_audit``) build their DAG with this one helper, so
    the fan-out/fan-in shape can never drift between the two entry points.

    ``aggregate`` lets callers override the fan-in reducer (default ``ALL``):
    use ``ANY`` for first-wins review paths, ``QUORUM`` for ">= N passed".
    """
    plan = ExecutionPlan(plan_id=plan_id, mode=mode)
    sink = PlanStep(
        id=sink_id,
        kind=AgentKind.EVIDENCE_REVIEWER,
        task=TaskEnvelope(kind="evidence_review"),
        strategy=StepStrategy.FAN_IN,
    )
    plan.add_step(sink)
    for leaf in leaves:
        plan.add_step(leaf)
        plan.link(leaf.id, sink.id, strategy=StepStrategy.FAN_IN, aggregate=aggregate)
    return plan


def leaf_subplan(plan: ExecutionPlan) -> ExecutionPlan:
    """The executable sub-plan: ``plan`` without its fan-in sink steps.

    Sink steps are realised by the caller (the supervisor / decomposer runs the
    :class:`EvidenceReviewerAgent` over the leaf results directly), so waves
    only ever contain the runnable leaves.
    """
    sub = ExecutionPlan(
        plan_id=f"{plan.plan_id}_leaves", mode=plan.mode, wave_policy=plan.wave_policy
    )
    for step in plan.steps.values():
        if step.strategy is StepStrategy.FAN_IN:
            continue
        sub.add_step(step.model_copy(deep=True))
    return sub


def _run_step(
    runner: AgentRunner, plan: ExecutionPlan, ctx: AgentRunContext, step_id: str
) -> AgentResult:
    step = plan.steps[step_id]
    return runner.run_child(ctx, step.kind, step.task)


__all__ = [
    "StepStrategy",
    "PlanStep",
    "PlanEdge",
    "ExecutionPlan",
    "WavePolicy",
    "FailurePolicy",
    "AggregateStrategy",
    "AggregateSpec",
    "CancellationToken",
    "PlanCancelled",
    "execution_waves",
    "PlanExecutionResult",
    "execute_plan",
    "fanout_fanin_plan",
    "leaf_subplan",
]
