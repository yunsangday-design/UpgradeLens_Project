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

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from upgradelens.agent.budget import BudgetLedger, default_budget_spec
from upgradelens.agent.checkpoint import CheckpointStore
from upgradelens.agent.dispatch import dispatch_by_task
from upgradelens.agent.evidence_reviewer import EvidenceReviewerAgent
from upgradelens.agent.execution_plan import (
    PlanStep,
    StepStrategy,
    execute_plan,
    fanout_fanin_plan,
    leaf_subplan,
)
from upgradelens.agent.router import Router
from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentRunContext,
    RunId,
    RunStatus,
    TaskEnvelope,
    new_run_id,
)
from upgradelens.agent.spec import default_registry
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
    # -- unified-runtime observability (MA-2-2 / MA-2-3; multi-agent only) -- #
    root_run_id: str = ""
    execution_plan: dict[str, Any] | None = None
    agent_runs: list[dict[str, Any]] = Field(default_factory=list)
    aggregate_result: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    # steps restored from a checkpoint instead of re-run (crash recovery)
    resumed_steps: list[str] = Field(default_factory=list)


def _envelope_from_task(
    kind: TaskKind, task: SoftwareTask, parent_id: str, idx: int
) -> TaskEnvelope:
    """Build the capability-agnostic :class:`TaskEnvelope` for one handoff."""
    c = task.context
    return TaskEnvelope(
        kind=kind.value,
        repo=getattr(c, "repo", "") or "",
        dependency=getattr(c, "dependency", "") or "",
        source_version=getattr(c, "source_version", "") or "",
        target_version=getattr(c, "target_version", "") or "",
        goal=task.goal,
        scope=getattr(c, "scope", "") or "",
        unified_diff=getattr(c, "unified_diff", "") or "",
        issue_text=getattr(c, "issue_text", "") or "",
        from_version=getattr(c, "from_version", "") or "",
        to_version=getattr(c, "to_version", "") or "",
        model=getattr(c, "model", "") or "",
        locale=task.locale or "zh-CN",
        max_turns=int(getattr(c, "max_turns", 0) or 0),
        extra={"task_id": f"{parent_id}-sub{idx}"},
    )


def _capability_view(result: AgentResult) -> CapabilityRunResult:
    """Bridge a unified :class:`AgentResult` back to the Workbench shape."""
    payload = result.to_capability_result()
    patch = payload.get("patch")
    if isinstance(patch, str):
        try:
            payload["patch"] = json.loads(patch)
        except (TypeError, ValueError):
            payload["patch"] = {"raw": patch}
    payload["status"] = "succeeded" if result.status is RunStatus.COMPLETED else "failed"
    return CapabilityRunResult.model_validate(payload)


def run_supervisor(
    task: SoftwareTask,
    ctx: AgentContext | None = None,
    *,
    mode: str = "fake",
    run_id: str | None = None,
    checkpoint_store: CheckpointStore | None = None,
) -> SupervisorResult:
    """Run ``task`` through the controlled Supervisor + Handoff layer.

    Single-capability requests are handed straight to ``dispatch_by_task`` (no
    needless multi-agent graph). Multi-capability requests fan out to one handoff
    per capability, each in an isolated context, then aggregate through a unified
    verification gate.

    Crash recovery: pass a ``checkpoint_store`` and re-dispatch with the same
    ``run_id`` -- already-completed leaves are restored from their checkpoints
    (surfaced via ``SupervisorResult.resumed_steps``) instead of re-executing.
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

    # Multi-agent (MA-2-2): fan out through the unified runtime as one parallel
    # wave of child runs, then converge through the deterministic aggregator +
    # shared evidence gate (MA-2-3). The DAG is the canonical fan-out/fan-in
    # template shared with the MA-4 decomposer; a re-dispatch of the same
    # ``run_id`` with a ``checkpoint_store`` resumes completed leaves instead of
    # re-executing them (crash recovery).
    ledger = BudgetLedger(default_budget_spec(ctx.budget_tokens or None))
    root_ctx = AgentRunContext(
        run_id=RunId(run_id) if run_id else new_run_id(),
        agent=AgentIdentity.create(AgentKind.SUPERVISOR),
        mode=ctx.mode,
        locale=task.locale or "zh-CN",
        budget=ledger,
    )
    runner = AgentRunner(default_registry())

    leaf_steps = [
        PlanStep(
            id=f"cap-{idx}-{kind.value}",
            kind=AgentKind(kind.value),
            task=_envelope_from_task(kind, task, parent_id, idx),
            strategy=StepStrategy.PARALLEL,
        )
        for idx, kind in enumerate(kinds)
    ]
    plan = fanout_fanin_plan(leaf_steps, str(root_ctx.run_id), mode=ctx.mode)
    leaf_plan = leaf_subplan(plan)

    exec_result = execute_plan(
        runner,
        leaf_plan,
        root_ctx,
        max_workers=max(1, len(leaf_plan.steps)),
        checkpoint_store=checkpoint_store,
    )
    leaf_results = [exec_result.results[sid] for sid in leaf_plan.steps]
    resumed_steps = list(exec_result.resumed_steps)

    # fan-in: one shared trust bar over every leaf's findings
    aggregate = EvidenceReviewerAgent().review(root_ctx, leaf_results)

    sub_results = [_capability_view(r) for r in leaf_results]
    used_budget = int(ledger.total.total)
    degradations: list[str] = []
    verification_passed = all(
        bool(r.verification and r.verification.get("passed")) for r in sub_results
    )
    if ctx.budget_tokens and used_budget > ctx.budget_tokens:
        degradations.append(
            f"budget-exceeded: used {used_budget} tokens > limit {ctx.budget_tokens}"
        )
        verification_passed = False
    conflicts = [dict(c) for c in aggregate.notes.get("conflicts", [])]
    if conflicts:
        degradations.append(f"conflicting-findings: {len(conflicts)} need human review")

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
        root_run_id=str(root_ctx.run_id),
        execution_plan=plan.model_dump(mode="json"),
        agent_runs=[r.model_dump(mode="json") for r in [*leaf_results, aggregate]],
        aggregate_result=aggregate.model_dump(mode="json"),
        conflicts=conflicts,
        resumed_steps=resumed_steps,
    )
