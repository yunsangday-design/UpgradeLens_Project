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
) -> AgentPlan:
    """Return the :class:`AgentPlan` for this run.

    Both live and fake modes use the deterministic ``DEFAULT_PLAN_STEPS`` so the
    artifact is always present, diffable, and free of the initial LLM planning
    round-trip (Step 13, #2.1). The ReAct loop still adapts at runtime through
    ad-hoc steps and coverage-driven supplementary retrieval, so no capability
    is lost versus the old LLM-ordered plan.
    """
    request_id_ = request_id
    mode_ = gateway.mode.value
    tgt_ = target_version or ""
    src_ = source_version or ""
    return AgentPlan(
        request_id=request_id_,
        mode=mode_,
        target_version_spec=tgt_,
        source_version_spec=src_,
        steps=_default_steps(repo_is_url),
    )
