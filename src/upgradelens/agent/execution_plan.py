"""Execution plan DAG, scheduler and supervised execution (MA-2-1 / MA-2-2).

A :class:`ExecutionPlan` is a capability-agnostic DAG of :class:`PlanStep` nodes.
Each step names an :class:`AgentKind` and a :class:`TaskEnvelope`; edges express
dependencies and the *strategy* a supervisor uses to expand/collect them:

* ``sequential`` -- one step after another (the default single-capability path);
* ``parallel`` -- independent steps in the same wave (bounded fan-out);
* ``fan_out`` -- a parent spawns several child agents that all read its output;
* ``fan_in`` -- several steps converge into one aggregator step.

:func:`execution_waves` topologically layers the DAG into waves that can run in
parallel; :func:`execute_plan` drives those waves through an
:class:`~upgradelens.agent.runner.AgentRunner` under one shared budget, so the
multi-agent supervisor reuses the single-capability engines unchanged.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.agent.checkpoint import CheckpointStore
from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
    RunStatus,
    TaskEnvelope,
)


class StepStrategy(StrEnum):
    """How a step relates to its predecessors."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"


class PlanStep(BaseModel):
    """One node in an execution DAG."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: AgentKind
    task: TaskEnvelope = Field(default_factory=TaskEnvelope)
    depends_on: list[str] = Field(default_factory=list)
    strategy: StepStrategy = StepStrategy.SEQUENTIAL
    max_children: int = 0


class PlanEdge(BaseModel):
    """A dependency edge between two steps, tagged with its strategy."""

    model_config = ConfigDict(extra="forbid")

    from_step: str
    to_step: str
    strategy: StepStrategy = StepStrategy.SEQUENTIAL


class ExecutionPlan(BaseModel):
    """A capability-agnostic DAG of agent steps."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    steps: dict[str, PlanStep] = Field(default_factory=dict)
    edges: list[PlanEdge] = Field(default_factory=list)
    mode: str = "fake"

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
    ) -> None:
        self.add_edge(PlanEdge(from_step=from_step, to_step=to_step, strategy=strategy))

    # -- queries ---------------------------------------------------------- #

    def roots(self) -> list[str]:
        """Steps with no predecessors."""
        has_parent = {e.to_step for e in self.edges}
        return [sid for sid in self.steps if sid not in has_parent]

    def successors(self, step_id: str) -> list[str]:
        return [e.to_step for e in self.edges if e.from_step == step_id]

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
    status: str = "completed"  # completed | failed
    # steps whose results were restored from a checkpoint instead of re-run
    resumed_steps: list[str] = field(default_factory=list)


def execute_plan(
    runner: AgentRunner,
    plan: ExecutionPlan,
    ctx: AgentRunContext,
    *,
    max_workers: int = 1,
    checkpoint_store: CheckpointStore | None = None,
) -> PlanExecutionResult:
    """Drive the plan's waves through ``runner`` under one shared budget.

    Within a wave, steps run concurrently (bounded by ``max_workers``) as *child*
    runs of ``ctx`` (so every leaf carries ``parent_run_id`` and the trace tree
    stays connected). Cost is recorded once, by the runner, into the shared
    (thread-safe) ledger on the context.

    With a ``checkpoint_store``, completed steps are checkpointed under
    ``(ctx.run_id, step_id)`` and a re-dispatch of the same run id resumes them
    losslessly instead of re-executing -- the crash-recovery guarantee. Checkpoint
    loads happen before the wave is dispatched and saves after it joins, so both
    in-memory and SQLite stores are used from the orchestrating thread only.
    """
    waves = execution_waves(plan)
    results: dict[str, AgentResult] = {}
    resumed: list[str] = []
    any_failed = False

    for wave in waves:
        wave_results: dict[str, AgentResult] = {}

        # 1) resume completed steps from checkpoints (main thread, pre-dispatch)
        pending = list(wave)
        if checkpoint_store is not None:
            still_pending: list[str] = []
            for sid in pending:
                cp = checkpoint_store.load(ctx.run_id, sid)
                if cp is None or cp.state.get("status") != RunStatus.COMPLETED.value:
                    still_pending.append(sid)
                    continue
                wave_results[sid] = AgentResult.model_validate(cp.state["result"])
                resumed.append(sid)
                # resumed costs were spent in the crashed attempt; keep the
                # ledger honest about the task's cumulative consumption
                if ctx.budget is not None and hasattr(ctx.budget, "record"):
                    ctx.budget.record(wave_results[sid].cost)
            pending = still_pending

        # 2) run the rest of the wave (serially or concurrently)
        if max_workers <= 1 or len(pending) <= 1:
            for sid in pending:
                wave_results[sid] = _run_step(runner, plan, ctx, sid)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_run_step, runner, plan, ctx, sid): sid for sid in pending}
                for fut in futures:
                    sid = futures[fut]
                    wave_results[sid] = fut.result()

        # 3) checkpoint newly completed steps (main thread, post-join)
        if checkpoint_store is not None:
            from upgradelens.agent.runtime import Checkpoint

            for sid in pending:
                res = wave_results[sid]
                if res.status is RunStatus.COMPLETED:
                    dump = res.model_dump(mode="json")
                    checkpoint_store.save(
                        Checkpoint(
                            run_id=ctx.run_id,
                            step=sid,
                            state={
                                "status": RunStatus.COMPLETED.value,
                                "result": dump,
                            },
                            state_hash=_hash_result(res),
                        )
                    )

        for sid, res in wave_results.items():
            results[sid] = res
            if res.status.value != "completed":
                any_failed = True

    return PlanExecutionResult(
        plan_id=plan.plan_id,
        results=results,
        waves=waves,
        status="failed" if any_failed else "completed",
        resumed_steps=resumed,
    )


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
) -> ExecutionPlan:
    """The canonical multi-agent template: independent parallel leaves
    converging on one evidence-review fan-in step.

    Both the Supervisor (classified multi-capability requests) and the
    constrained :class:`~upgradelens.agent.decomposer.DynamicDecomposer`
    (meta goals like ``full_audit``) build their DAG with this one helper, so
    the fan-out/fan-in shape can never drift between the two entry points.
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
        plan.link(leaf.id, sink.id, strategy=StepStrategy.FAN_IN)
    return plan


def leaf_subplan(plan: ExecutionPlan) -> ExecutionPlan:
    """The executable sub-plan: ``plan`` without its fan-in sink steps.

    Sink steps are realised by the caller (the supervisor / decomposer runs the
    :class:`EvidenceReviewerAgent` over the leaf results directly), so waves
    only ever contain the runnable leaves.
    """
    sub = ExecutionPlan(plan_id=f"{plan.plan_id}_leaves", mode=plan.mode)
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
    "execution_waves",
    "PlanExecutionResult",
    "execute_plan",
    "fanout_fanin_plan",
    "leaf_subplan",
]
