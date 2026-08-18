"""Constrained dynamic decomposition: task -> execution DAG (MA-4).

Given a top-level request, the :class:`DynamicDecomposer` breaks it into the
*known* professional agents (never invents a new agent kind) and wires them as
a fan-out of leaf capabilities converging on an :class:`EvidenceReviewerAgent`
fan-in. Decomposition is **constrained**: a kind is only included if it is
registered, and the only meta-task recognised is ``full_audit`` (cross-cutting
PR + security review, plus dependency upgrade when dependencies are present).
"""

from __future__ import annotations

from upgradelens.agent.evidence_reviewer import EvidenceReviewerAgent, register_evidence_reviewer
from upgradelens.agent.execution_plan import (
    ExecutionPlan,
    PlanStep,
    StepStrategy,
    execute_plan,
)
from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
    TaskEnvelope,
)
from upgradelens.agent.spec import AgentRegistry


def _leaf_kinds(task: TaskEnvelope, registry: AgentRegistry) -> list[AgentKind]:
    """Constrained leaf selection from a task description."""
    if task.kind == "full_audit":
        leaves = [AgentKind.PR_REVIEW, AgentKind.SECURITY_REVIEW]
        if task.extra.get("has_dependencies"):
            leaves.append(AgentKind.DEPENDENCY_UPGRADE)
    else:
        try:
            leaves = [AgentKind(task.kind)]
        except ValueError:
            leaves = [AgentKind.PR_REVIEW]
    # constrain to registered, runnable kinds
    return [k for k in leaves if registry.get(k) is not None]


class DynamicDecomposer:
    """Decompose a request into a fan-out/fan-in execution DAG."""

    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    def decompose(self, task: TaskEnvelope, *, plan_id: str = "plan") -> ExecutionPlan:
        register_evidence_reviewer(self.registry)
        leaves = _leaf_kinds(task, self.registry)
        if not leaves:
            leaves = [AgentKind.PR_REVIEW]

        plan = ExecutionPlan(plan_id=plan_id, mode=task.locale or "fake")
        for kind in leaves:
            plan.add_step(
                PlanStep(
                    id=f"leaf_{kind.value}",
                    kind=kind,
                    task=TaskEnvelope(kind=kind.value, repo=task.repo, goal=task.goal),
                    strategy=StepStrategy.FAN_OUT,
                )
            )
        sink = PlanStep(
            id="evidence_review",
            kind=AgentKind.EVIDENCE_REVIEWER,
            task=TaskEnvelope(kind="evidence_review"),
            strategy=StepStrategy.FAN_IN,
        )
        plan.add_step(sink)
        for kind in leaves:
            plan.link(f"leaf_{kind.value}", "evidence_review", strategy=StepStrategy.FAN_IN)
        return plan

    def decompose_and_run(
        self, task: TaskEnvelope, ctx: AgentRunContext, *, max_workers: int = 1
    ) -> tuple[AgentResult, ExecutionPlan]:
        """Decompose, run the leaves, then run the evidence reviewer over them."""
        register_evidence_reviewer(self.registry)
        plan = self.decompose(task)
        leaf_plan = ExecutionPlan(plan_id=f"{plan.plan_id}_leaves", mode=plan.mode)
        for sid, step in plan.steps.items():
            if sid == "evidence_review":
                continue
            leaf_plan.add_step(step)

        leaf_exec = execute_plan(self.runner(), leaf_plan, ctx, max_workers=max_workers)
        reviewer = EvidenceReviewerAgent()
        reviewed = reviewer.review(ctx, list(leaf_exec.results.values()))
        return reviewed, plan

    def runner(self) -> AgentRunner:
        return AgentRunner(self.registry)


__all__ = ["DynamicDecomposer", "_leaf_kinds"]
