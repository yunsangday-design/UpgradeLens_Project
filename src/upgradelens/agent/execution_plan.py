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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
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


def execute_plan(
    runner: AgentRunner,
    plan: ExecutionPlan,
    ctx: AgentRunContext,
    *,
    max_workers: int = 1,
) -> PlanExecutionResult:
    """Drive the plan's waves through ``runner`` under one shared budget.

    Within a wave, steps run concurrently (bounded by ``max_workers``); cost is
    recorded into the shared ledger *after* the wave finishes, so concurrent
    runs never race on the ledger.
    """
    waves = execution_waves(plan)
    results: dict[str, AgentResult] = {}
    any_failed = False

    for wave in waves:
        wave_results: dict[str, AgentResult] = {}

        if max_workers <= 1 or len(wave) <= 1:
            for sid in wave:
                wave_results[sid] = _run_step(runner, plan, ctx, sid)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_run_step, runner, plan, ctx, sid): sid for sid in wave}
                for fut in futures:
                    sid = futures[fut]
                    wave_results[sid] = fut.result()

        for sid, res in wave_results.items():
            results[sid] = res
            if res.status.value != "completed":
                any_failed = True
            if ctx.budget is not None and hasattr(ctx.budget, "record"):
                ctx.budget.record(res.cost)

    return PlanExecutionResult(
        plan_id=plan.plan_id,
        results=results,
        waves=waves,
        status="failed" if any_failed else "completed",
    )


def _run_step(
    runner: AgentRunner, plan: ExecutionPlan, ctx: AgentRunContext, step_id: str
) -> AgentResult:
    step = plan.steps[step_id]
    return runner.run(step.kind, ctx, step.task)


__all__ = [
    "StepStrategy",
    "PlanStep",
    "PlanEdge",
    "ExecutionPlan",
    "execution_waves",
    "PlanExecutionResult",
    "execute_plan",
]
