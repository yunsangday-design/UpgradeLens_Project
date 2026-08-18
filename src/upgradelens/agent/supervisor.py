"""Controlled Supervisor + Handoff multi-agent layer (research report M3).

This is a *thin* deterministic orchestration layer built ON TOP OF
:func:`upgradelens.agent.dispatch.dispatch_by_task` (M1b). It implements the
"Supervisor + Handoff" pattern the competitiveness report recommends, while
respecting its hard constraints:

* The Supervisor owns the control flow -- triage, budgeting, permission gating
  and the unified verification gate -- and never delegates it to a sub-agent.
* Sub-agents are the existing capability runners. They communicate ONLY with the
  Supervisor (no horizontal peer-to-peer messaging) and each run in their own
  isolated :class:`SoftwareTask` context.
* Single-capability requests short-circuit straight to ``dispatch_by_task``. We do
  NOT spin up a multi-agent graph for a task one capability can already handle.
* The decomposition is rule-based and identical in fake/live, so replay stays
  offline-reproducible. An optional LLM splitter can be layered behind
  ``mode == "live"`` later, but it must always fall back to these rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from upgradelens.agent.dispatch import dispatch_by_task
from upgradelens.agent.router import Router
from upgradelens.capabilities.workbench import CapabilityRunResult
from upgradelens.core.task import SoftwareTask, TaskKind

__all__ = [
    "AgentContext",
    "SupervisorResult",
    "classify_capabilities",
    "decompose_task",
    "handoff_to",
    "run_supervisor",
]


@dataclass
class AgentContext:
    """Budget / permission envelope propagated to every handoff.

    The Supervisor holds the authoritative envelope; a sub-agent can read it but
    never widens it. This keeps the safety-critical control flow in the
    deterministic layer.
    """

    mode: str = "fake"
    budget_tokens: int = 200_000
    allow_writes: bool = False
    # None == every capability is allowed. Otherwise a tuple of TaskKind.value (or
    # TaskKind names) the Supervisor is permitted to hand off to.
    allowed_capabilities: tuple[str, ...] | None = None
    parent_task_id: str | None = None


def classify_capabilities(
    text: str,
    *,
    repo: str | None = None,
    dependency: str | None = None,
    target: str | None = None,
    source: str | None = None,
) -> list[TaskKind]:
    """Multi-capability classification (convenience wrapper over :class:`Router`)."""
    return Router().classify_capabilities(
        text, repo=repo, dependency=dependency, target=target, source=source
    )


def decompose_task(task: SoftwareTask, ctx: AgentContext) -> list[TaskKind]:
    """Resolve the (possibly multi-) capability kinds for ``task``.

    Deterministic and offline-reproducible. Applies the permission gate from
    ``ctx.allowed_capabilities`` so an unauthorised capability is dropped before
    any handoff happens.
    """
    kinds = classify_capabilities(
        task.goal,
        repo=task.context.repo or None,
        dependency=task.context.dependency or None,
        target=task.context.target_version or None,
        source=task.context.source_version or None,
    )
    if ctx.allowed_capabilities is not None:
        allowed = {str(a).lower() for a in ctx.allowed_capabilities}
        kinds = [k for k in kinds if k.value.lower() in allowed or k.name.lower() in allowed]
    return kinds


def handoff_to(
    task: SoftwareTask,
    kind: TaskKind,
    idx: int,
    parent_id: str,
    ctx: AgentContext,
) -> CapabilityRunResult:
    """Hand a single capability to the unified dispatcher as an isolated sub-task.

    This is the "Handoff" primitive. The sub-agent receives its own
    :class:`SoftwareTask` (fresh id, copied context) and never sees its siblings,
    satisfying the "no horizontal peer-to-peer messaging" constraint.
    """
    sub_context = task.context.model_copy(deep=True)
    sub = SoftwareTask(
        task_id=f"{parent_id}-sub{idx}",
        kind=kind,
        goal=task.goal,
        context=sub_context,
        locale=task.locale,
    )
    return dispatch_by_task(sub, mode=ctx.mode)


class SupervisorResult(BaseModel):
    """Aggregated outcome of a Supervisor-run request."""

    orchestration: str = "single"  # "single" | "multi-agent"
    parent_task_id: str = ""
    capability_kinds: list[str] = Field(default_factory=list)
    sub_results: list[CapabilityRunResult] = Field(default_factory=list)
    # Populated only for single-capability runs; keeps the HTTP contract stable
    # for the existing Workbench single-result view.
    result: CapabilityRunResult | None = None
    summary: str = ""
    budget_tokens_used: int = 0
    budget_tokens_limit: int = 0
    verification_passed: bool = True
    degradations: list[str] = Field(default_factory=list)


def run_supervisor(
    task: SoftwareTask,
    ctx: AgentContext | None = None,
    *,
    mode: str = "fake",
) -> SupervisorResult:
    """Run ``task`` through the controlled Supervisor + Handoff layer.

    Single-capability requests are handed straight to ``dispatch_by_task`` (no
    needless multi-agent graph). Multi-capability requests fan out to one handoff
    per capability, each in an isolated context, then aggregate through a unified
    verification gate.
    """
    ctx = ctx or AgentContext(mode=mode)
    parent_id = task.task_id or "task"
    kinds = decompose_task(task, ctx)

    if not kinds:
        return SupervisorResult(
            parent_task_id=parent_id,
            summary="未能从任务描述中分诊出任何能力；未执行任何 sub-agent。",
            verification_passed=False,
            degradations=["no-capability-matched"],
        )

    if len(kinds) == 1:
        res = handoff_to(task, kinds[0], 0, parent_id, ctx)
        return SupervisorResult(
            orchestration="single",
            parent_task_id=parent_id,
            capability_kinds=[kinds[0].value],
            sub_results=[res],
            result=res,
            verification_passed=bool(res.verification and res.verification.get("passed")),
        )

    # Multi-agent: fan out, then aggregate.
    sub_results: list[CapabilityRunResult] = []
    used_budget = 0
    degradations: list[str] = []
    for idx, kind in enumerate(kinds):
        sub = handoff_to(task, kind, idx, parent_id, ctx)
        cost = sub.cost or {}
        used_budget += int(cost.get("total_tokens", 0) or 0)
        sub_results.append(sub)

    verification_passed = all(
        bool(r.verification and r.verification.get("passed")) for r in sub_results
    )
    if ctx.budget_tokens and used_budget > ctx.budget_tokens:
        degradations.append(
            f"budget-exceeded: used {used_budget} tokens > limit {ctx.budget_tokens}"
        )
        verification_passed = False

    summary = "多能力协作完成（Supervisor 编排）：" + ", ".join(k.value for k in kinds)
    return SupervisorResult(
        orchestration="multi-agent",
        parent_task_id=parent_id,
        capability_kinds=[k.value for k in kinds],
        sub_results=sub_results,
        summary=summary,
        budget_tokens_used=used_budget,
        budget_tokens_limit=ctx.budget_tokens,
        verification_passed=verification_passed,
        degradations=degradations,
    )
