"""Agent specification, registry and built-in professional agents (MA-1B-1).

An :class:`AgentSpec` is the *declaration* of a professional agent: who it is,
what capability it wraps, and the contract it obeys. The 5 professional agents
(dependency upgrade / PR review / issue repair / security review / breaking
change) are thin, uniform wrappers over the existing single-capability runners
in :mod:`upgradelens.capabilities`, so the multi-agent runtime reuses proven
code instead of forking it.

The :class:`AgentRegistry` answers the supervisor's "which agent runs kind X?"
question, and :func:`default_registry` wires up the built-ins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from upgradelens.agent.runtime import (
    DEFAULT_PERMISSIONS,
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentRunContext,
    CostUsage,
    RunStatus,
    TaskEnvelope,
)
from upgradelens.core.finding import Finding
from upgradelens.core.verification import VerificationResult

# ---------------------------------------------------------------------------
# Agent function contract
# ---------------------------------------------------------------------------


class AgentFn(Protocol):
    """A professional agent: run a :class:`TaskEnvelope` under a context."""

    def __call__(self, ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult: ...


# ---------------------------------------------------------------------------
# Specification
# ---------------------------------------------------------------------------


@dataclass
class AgentSpec:
    """The declaration of one professional agent."""

    agent_id: str
    kind: AgentKind
    name: str
    version: str = "1.0.0"
    description: str = ""
    run: AgentFn | None = None  # callable implementation
    required_permissions: frozenset[str] = DEFAULT_PERMISSIONS
    max_children: int = 0  # 0 == leaf agent (no sub-agents)
    max_turns: int = 0  # 0 == use context default

    def identity(self) -> AgentIdentity:
        return AgentIdentity.create(self.kind, version=self.version, name=self.name)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Maps an :class:`AgentKind` to its :class:`AgentSpec`."""

    def __init__(self) -> None:
        self._specs: dict[AgentKind, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        if spec.run is None:
            raise ValueError(f"agent spec {spec.agent_id!r} has no run implementation")
        self._specs[spec.kind] = spec

    def get(self, kind: AgentKind) -> AgentSpec | None:
        return self._specs.get(kind)

    def resolve(self, kind: AgentKind) -> AgentSpec:
        spec = self._specs.get(kind)
        if spec is None:
            raise KeyError(f"no agent registered for kind {kind.value!r}")
        return spec

    def list_specs(self) -> list[AgentSpec]:
        return list(self._specs.values())

    def kinds(self) -> list[AgentKind]:
        return list(self._specs)


# ---------------------------------------------------------------------------
# Capability -> agent adapter
# ---------------------------------------------------------------------------

# The 5 professional capabilities the runtime can drive today.
PROFESSIONAL_KINDS: tuple[AgentKind, ...] = (
    AgentKind.DEPENDENCY_UPGRADE,
    AgentKind.PR_REVIEW,
    AgentKind.ISSUE_REPAIR,
    AgentKind.SECURITY_REVIEW,
    AgentKind.BREAKING_CHANGE,
)


def _safe_finding(raw: dict[str, Any]) -> Finding | None:
    """Validate a capability finding dict, downgrading VERIFIED-without-evidence."""
    try:
        return Finding.model_validate(raw)
    except ValidationError:
        fixed = dict(raw)
        if fixed.get("status") == "verified" and not fixed.get("evidence_ids"):
            fixed["status"] = "candidate"
        try:
            return Finding.model_validate(fixed)
        except ValidationError:
            return None


def _agent_result_from_capability(
    cap: Any, ctx: AgentRunContext
) -> AgentResult:
    """Bridge a :class:`CapabilityRunResult` into a unified :class:`AgentResult`."""
    findings = [f for f in (_safe_finding(x) for x in cap.findings) if f is not None]

    verification: VerificationResult | None = None
    if cap.verification:
        vd = dict(cap.verification)
        vd.setdefault("proposal_id", ctx.run_id)
        try:
            verification = VerificationResult.model_validate(vd)
        except ValidationError:
            verification = None

    patch: str | None = None
    if cap.patch:
        patch = (
            cap.patch if isinstance(cap.patch, str) else json.dumps(cap.patch, ensure_ascii=False)
        )

    cost = (
        CostUsage.from_ledger_entry(cap.cost)
        if isinstance(cap.cost, dict)
        else CostUsage()
    )

    status = RunStatus.COMPLETED if cap.status == "succeeded" else RunStatus.FAILED
    return AgentResult(
        run_id=ctx.run_id,
        parent_run_id=ctx.parent_run_id,
        agent_id=ctx.agent.agent_id,
        kind=ctx.agent.kind,
        status=status,
        summary=cap.summary,
        findings=findings,
        action_proposals=list(cap.action_proposals),
        verification=verification,
        coverage=cap.coverage or {},
        test_results=list(cap.test_results),
        patch=patch,
        degradations=list(cap.degradations),
        trace=list(cap.trace),
        cost=cost,
        notes={"capability_meta": cap.capability_meta, "error": cap.error},
    )


def _resolve_skill_digest(kind: AgentKind, locale: str) -> dict[str, Any] | None:
    """Resolve the AgentSkill for ``kind``/``locale`` into a compact digest (SK-1-3).

    Progressive disclosure level 1: the agent receives the behaviour spec
    (steps / constraints / completion criteria), never the full markdown body.
    The digest is echoed into the result notes so the UI can attribute the run
    to the behaviour spec that governed it.
    """
    try:
        from upgradelens.agent_skills.resolver import resolve_agent_skill
    except Exception:  # pragma: no cover - registry failure must not kill the run
        return None
    skill = resolve_agent_skill(kind.value, locale=locale or "en")
    if skill is None:
        return None
    return {
        "skill_id": skill.skill_id,
        "version": skill.version,
        "language": skill.language,
        "steps": list(skill.steps),
        "constraints": list(skill.constraints),
        "completion_criteria": list(skill.completion_criteria),
        "instructions": skill.to_instructions(),
    }


def make_capability_agent(kind: AgentKind, *, version: str = "1.0.0") -> AgentSpec:
    """Wrap an existing single-capability runner as an :class:`AgentSpec`."""

    def _run(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
        from upgradelens.capabilities.workbench import run_capability
        from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

        tc = TaskContext(**task.to_task_context())
        st = SoftwareTask(task_id=ctx.run_id, kind=TaskKind(kind.value), context=tc)
        cap = run_capability(st, gateway=ctx.gateway, mode=ctx.mode)
        result = _agent_result_from_capability(cap, ctx)
        skill_digest = _resolve_skill_digest(kind, task.locale)
        if skill_digest is not None:
            result.notes["agent_skill"] = {
                k: v for k, v in skill_digest.items() if k != "instructions"
            }
        return result

    return AgentSpec(
        agent_id=kind.value,
        kind=kind,
        name=kind.value.replace("_", " ").title(),
        version=version,
        description=f"Professional agent wrapping the {kind.value} capability.",
        run=_run,
        max_children=0,
    )


def default_registry() -> AgentRegistry:
    """Wire up the built-in professional agents."""
    reg = AgentRegistry()
    for kind in PROFESSIONAL_KINDS:
        reg.register(make_capability_agent(kind))
    return reg


__all__ = [
    "AgentFn",
    "AgentSpec",
    "AgentRegistry",
    "PROFESSIONAL_KINDS",
    "make_capability_agent",
    "default_registry",
    "_agent_result_from_capability",
]
