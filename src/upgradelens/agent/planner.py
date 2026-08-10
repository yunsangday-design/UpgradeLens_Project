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

from pydantic import BaseModel, Field

from upgradelens.agent.plan import PENDING, AgentPlan, AgentPlanStep
from upgradelens.agent.run_store import DEFAULT_PLAN_STEPS
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.tools.registry import ToolRegistry


class _PlannerStep(BaseModel):
    """One step the model proposes (minimal; we enrich it into AgentPlanStep)."""

    tool: str = Field(description="The capability to invoke.")
    purpose: str = Field(default="", description="Why this step matters.")


class _PlannerResponse(BaseModel):
    steps: list[_PlannerStep] = Field(default_factory=list)


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
) -> AgentPlan:
    """Return the :class:`AgentPlan` for this run.

    Live mode asks the model to order the available tools; fake mode returns the
    deterministic linear plan. Planning is best-effort: any failure degrades to
    the default plan rather than blocking the run.
    """
    request_id_ = request_id
    mode_ = gateway.mode.value
    tgt_ = target_version or ""
    src_ = source_version or ""
    if gateway.mode == ModelMode.FAKE:
        return AgentPlan(
            request_id=request_id_,
            mode=mode_,
            target_version_spec=tgt_,
            source_version_spec=src_,
            steps=_default_steps(repo_is_url),
        )

    tool_specs = "\n".join(f"- {spec['name']}: {spec['description']}" for spec in registry.specs())
    prompt = (
        "You are planning an upgrade-impact assessment. Given the available tools, "
        "return the ORDERED list of steps needed to collect evidence for a "
        f"dependency upgrade. Keep only tools that are actually useful.\n\n"
        f"Dependency: {dependency}\n"
        f"Target version spec: {target_version or '(unspecified)'}\n"
        f"Source version spec: {source_version or '(unknown)'}\n"
        f"Repo: {repo}\n\n"
        "Available tools:\n"
        f"{tool_specs}\n"
    )
    try:
        plan, _ = gateway.complete_structured(
            prompt=prompt, schema=_PlannerResponse, name="agent_planner"
        )
    except Exception:  # noqa: BLE001 - planning must never block the run
        plan = None
    if plan is not None and plan.steps:
        steps: list[AgentPlanStep] = []
        for i, step in enumerate(plan.steps, start=1):
            steps.append(
                AgentPlanStep(
                    id=f"s{i}",
                    tool=step.tool,
                    seq=i,
                    status=PENDING,
                    phase="collect",
                    reason=step.purpose,
                )
            )
        return AgentPlan(
            request_id=request_id_,
            mode=mode_,
            target_version_spec=tgt_,
            source_version_spec=src_,
            steps=steps,
        )

    return AgentPlan(
        request_id=request_id_,
        mode=mode_,
        target_version_spec=tgt_,
        source_version_spec=src_,
        steps=_default_steps(repo_is_url),
    )
