"""ROADMAP Step 3 -- Planner for the agent run.

Produces the :class:`~upgradelens.agent.plan.AgentPlan` payload (the ordered
list of steps the run will follow). In live mode the LLM is asked to order the
available tools; in fake mode (and on any planning failure) a deterministic
linear plan is returned so the artifact is always present and diffable.

The plan is *live*: the ReAct loop updates each step's status as it executes
(``pending`` -> ``running`` -> ``succeeded``/``failed``/``skipped``), and any
tool the model calls that is not already in the plan is recorded as an ad-hoc
step. The plan is written back atomically after every update so a crash leaves
a coherent state.
"""

from __future__ import annotations

from upgradelens.agent.plan import PENDING, AgentPlan, AgentPlanStep
from upgradelens.agent.run_store import DEFAULT_PLAN_STEPS
from upgradelens.core.capability import CapabilityPlan, CapabilityRegistry
from upgradelens.core.task import SoftwareTask
from upgradelens.llm.gateway import ModelGateway
from upgradelens.tools.registry import ToolRegistry


def _default_steps(repo_is_url: bool) -> list[AgentPlanStep]:
    """Deterministic plan steps, dropping ``clone_repo`` for a local repo."""
    seq = 0
    out: list[AgentPlanStep] = []
    for raw in DEFAULT_PLAN_STEPS:
        tool = raw["tool"]
        if tool == "clone_repo" and not repo_is_url:
            continue
        seq += 1
        out.append(
            AgentPlanStep(
                id=f"s{seq}",
                tool=tool,
                seq=seq,
                status=PENDING,
                phase=raw.get("phase", "collect"),
                reason=raw.get("purpose", ""),
            )
        )
    return out


def build_agent_plan(
    *,
    gateway: ModelGateway,
    registry: ToolRegistry,
    repo: str,
    dependency: str,
    target_version: str | None,
    source_version: str | None = None,
    request_id: str = "",
    repo_is_url: bool = True,
    task: SoftwareTask | None = None,
    capability_registry: CapabilityRegistry | None = None,
) -> AgentPlan:
    """Return the :class:`AgentPlan` for this run.

    Both live and fake modes use the deterministic ``DEFAULT_PLAN_STEPS`` so the
    artifact is always present, diffable, and free of the initial LLM planning
    round-trip (Step 13, #2.1). The ReAct loop still adapts at runtime through
    ad-hoc steps and coverage-driven supplementary retrieval, so no capability
    is lost versus the old LLM-ordered plan.

    S1 integration: when a ``SoftwareTask`` and a capability registry are provided,
    a matching capability may override the step list via ``build_plan``. Unknown
    task kinds fall back to the deterministic default.
    """
    request_id_ = request_id
    mode_ = gateway.mode.value
    tgt_ = target_version or ""
    src_ = source_version or ""
    steps = _default_steps(repo_is_url)
    if task is not None and capability_registry is not None:
        cap = capability_registry.get(str(task.kind))
        if cap is not None:
            cap_plan = cap.build_plan(task)
            steps = _steps_from_capability(cap_plan, repo_is_url)
    return AgentPlan(
        request_id=request_id_,
        mode=mode_,
        target_version_spec=tgt_,
        source_version_spec=src_,
        steps=steps,
    )


def _steps_from_capability(plan: CapabilityPlan, repo_is_url: bool) -> list[AgentPlanStep]:
    """Convert a capability's ordered ``steps`` (tool names) into plan steps."""
    seq = 0
    out: list[AgentPlanStep] = []
    for name in plan.steps:
        if name == "clone_repo" and not repo_is_url:
            continue
        seq += 1
        out.append(
            AgentPlanStep(
                id=f"s{seq}",
                tool=name,
                seq=seq,
                status=PENDING,
                phase=str(plan.capability_kind),
                reason=plan.note or "",
            )
        )
    return out
