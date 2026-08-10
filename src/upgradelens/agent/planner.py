"""ROADMAP Step 3 — LLM planner for the agent run.

Produces the ``plan.json`` payload (the ordered list of tools/steps the run
will follow). In live mode the LLM is asked to order the available tools into a
sequence; in fake mode (and on any planning failure) a deterministic linear
plan is returned so the artifact is always present and diffable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from upgradelens.agent.run_store import DEFAULT_PLAN_STEPS
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.tools.registry import ToolRegistry


class _AgentPlanStep(BaseModel):
    order: int = Field(default=0)
    tool: str = Field(default="")
    purpose: str = Field(default="")


class _AgentPlan(BaseModel):
    steps: list[_AgentPlanStep] = Field(default_factory=list)


def build_agent_plan(
    *,
    gateway: ModelGateway,
    registry: ToolRegistry,
    repo: str,
    dependency: str,
    target_version: str | None,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Return the ``plan.json`` payload for this run.

    Live mode asks the model to order the available tools; fake mode returns the
    deterministic linear plan. Planning is best-effort: any failure degrades to
    the default plan rather than blocking the run.
    """
    if gateway.mode == ModelMode.FAKE:
        return {"steps": [dict(step) for step in DEFAULT_PLAN_STEPS]}

    tool_specs = "\n".join(f"- {spec['name']}: {spec['description']}" for spec in registry.specs())
    prompt = (
        "You are planning a dependency-upgrade assessment. Given the task and the "
        "available tools, output the ordered list of tool calls needed to gather "
        "evidence and produce the assessment.\n\n"
        f"Task: upgrade `{dependency}` to `{target_version}`"
        f"{(' (source ' + source_version + ')') if source_version else ''}.\n"
        f"Repository: {repo}\n\n"
        "Available tools:\n"
        f"{tool_specs}\n\n"
        "Rules:\n"
        "- clone_repo must come first (it yields the local path the other tools need).\n"
        "- scan_code and resolve_skill usually follow.\n"
        "- retrieve_docs is optional: skip it when no doc store is configured.\n"
        "- Return the steps as an ordered list; omit verify_report/run_assessment "
        "(the harness runs the analysis automatically)."
    )
    try:
        plan, _ = gateway.complete_structured(
            prompt=prompt, schema=_AgentPlan, name="agent_planner"
        )
        if plan.steps:
            return {
                "steps": [
                    {"order": index + 1, "tool": step.tool, "purpose": step.purpose}
                    for index, step in enumerate(plan.steps)
                ]
            }
    except Exception:  # planning is best-effort; never block the run on it
        pass
    return {"steps": [dict(step) for step in DEFAULT_PLAN_STEPS]}
