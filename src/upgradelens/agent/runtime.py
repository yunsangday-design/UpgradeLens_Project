"""Unified multi-agent runtime contracts (MA-1A-1).

This module defines the *shared language* every professional agent speaks, so
that the supervisor, the execution planner and the result aggregator can treat
all capabilities uniformly:

- :class:`RunId` -- a stable identifier for one agent run (and its children);
- :class:`AgentIdentity` -- who is running (kind + version), for attribution;
- :class:`RunStatus` -- the lifecycle states a run moves through;
- :class:`CostUsage` -- the *single* cost contract every run reports;
- :class:`AgentRunContext` -- the unified context handed to every agent
  (run id, optional parent, budget ledger, permissions, locale, trace);
- :class:`LifecycleEvent` / :class:`Checkpoint` / :class:`TraceNode` -- the
  observability primitives the supervisor and UI consume;
- :class:`AgentResult` -- the unified, capability-agnostic result object.

These models are intentionally additive: the existing single-capability
:class:`~upgradelens.agent.api.AgentResult` and
:class:`~upgradelens.capabilities.workbench.CapabilityRunResult` keep working;
the :func:`AgentResult.to_capability_result` adapter bridges them so the workbench
needs only one shape.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, NewType

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.core.finding import Finding, Severity
from upgradelens.core.verification import VerificationResult

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

RunId = NewType("RunId", str)


def new_run_id() -> RunId:
    """Generate a short, collision-resistant run id (8 hex chars)."""
    return RunId(uuid.uuid4().hex[:8])


# ---------------------------------------------------------------------------
# Agent identity & lifecycle
# ---------------------------------------------------------------------------


class AgentKind(StrEnum):
    """The family of agent a run belongs to.

    Mirrors the capability registry kinds so a run can be attributed to a
    concrete capability without coupling the runtime to the capability package.
    """

    SUPERVISOR = "supervisor"
    DEPENDENCY_UPGRADE = "dependency_upgrade"
    PR_REVIEW = "pr_review"
    ISSUE_REPAIR = "issue_repair"
    SECURITY_REVIEW = "security_review"
    BREAKING_CHANGE = "breaking_change"
    EVIDENCE_REVIEWER = "evidence_reviewer"
    RESULT_AGGREGATOR = "result_aggregator"
    GENERIC = "generic"


class RunStatus(StrEnum):
    """Lifecycle states a run passes through."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"  # blocked on a child / human input
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentIdentity(BaseModel):
    """Who is executing a run, for attribution and routing."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    kind: AgentKind
    name: str = ""
    version: str = "1.0.0"
    description: str = ""

    @classmethod
    def create(cls, kind: AgentKind, *, version: str = "1.0.0", name: str = "") -> AgentIdentity:
        kind_value = kind.value
        return cls(
            agent_id=kind_value,
            kind=kind,
            name=name or kind_value,
            version=version,
        )


# ---------------------------------------------------------------------------
# Cost contract (the single shape every run reports)
# ---------------------------------------------------------------------------


class CostUsage(BaseModel):
    """The unified cost/token contract for one run or one ledger entry.

    Field names are aligned with the existing
    :class:`~upgradelens.agent.loop.Ledger` entry dict so conversion is lossless.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total tokens actually accounted for (falls back to the explicit field)."""
        if self.total_tokens:
            return self.total_tokens
        return self.input_tokens + self.output_tokens + self.cache_read_tokens

    def merge(self, other: CostUsage) -> CostUsage:
        """Return a new :class:`CostUsage` summing two usages (immutable)."""
        return CostUsage(
            model=self.model or other.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            total_tokens=self.total + other.total,
            tool_calls=self.tool_calls + other.tool_calls,
            latency_ms=self.latency_ms + other.latency_ms,
            cost_usd=self.cost_usd + other.cost_usd,
            extra={**self.extra, **other.extra},
        )

    @classmethod
    def from_ledger_entry(cls, entry: dict[str, Any], *, model: str = "") -> CostUsage:
        """Build a :class:`CostUsage` from an existing ledger entry dict."""
        return cls(
            model=model or str(entry.get("model", "")),
            input_tokens=int(entry.get("input_tokens", 0)),
            output_tokens=int(entry.get("output_tokens", 0)),
            cache_read_tokens=int(entry.get("cache_read_tokens", 0)),
            total_tokens=int(entry.get("total_tokens", 0)),
            tool_calls=int(entry.get("tool_calls", 0)),
            latency_ms=float(entry.get("latency_ms", 0.0)),
            cost_usd=float(entry.get("cost_usd", 0.0)),
        )


# ---------------------------------------------------------------------------
# Run context (the unified object handed to every agent)
# ---------------------------------------------------------------------------

# Permissions a child agent may exercise. Default-deny; the supervisor grants
# only what a capability needs.
DEFAULT_PERMISSIONS: frozenset[str] = frozenset(
    {
        "read_repo",
        "retrieve_corpus",
        "use_tools",
    }
)


@dataclass
class AgentRunContext:
    """The unified context every professional agent receives.

    This replaces the ad-hoc ``AgentContext`` used by the supervisor and the
    loose ``run_id`` / budget threading in the dispatch layer. It is a plain
    dataclass (not pydantic) because it holds live references to the budget
    ledger, gateway and run store.
    """

    run_id: RunId
    agent: AgentIdentity
    parent_run_id: RunId | None = None
    mode: str = "fake"
    permissions: frozenset[str] = DEFAULT_PERMISSIONS
    locale: str = "zh-CN"
    trace_enabled: bool = True
    # Live references (not serialised):
    budget: Any | None = None  # BudgetLedger
    gateway: Any | None = None
    store: Any | None = None

    def has_permission(self, perm: str) -> bool:
        return perm in self.permissions

    def child(self, agent: AgentIdentity, *, run_id: RunId | None = None) -> AgentRunContext:
        """Spawn a child context inheriting budget, gateway, store and locale."""
        return AgentRunContext(
            run_id=run_id or new_run_id(),
            agent=agent,
            parent_run_id=self.run_id,
            mode=self.mode,
            permissions=self.permissions,
            locale=self.locale,
            trace_enabled=self.trace_enabled,
            budget=self.budget,
            gateway=self.gateway,
            store=self.store,
        )


# ---------------------------------------------------------------------------
# Task envelope (the capability-agnostic unit of work)
# ---------------------------------------------------------------------------


class TaskEnvelope(BaseModel):
    """A capability-agnostic unit of work the supervisor hands to an agent.

    It deliberately mirrors the fields of
    :class:`~upgradelens.core.task.TaskContext` so the supervisor can build it
    without knowing capability specifics; :func:`TaskEnvelope.to_task_context`
    materialises the capability-specific :class:`~upgradelens.core.task.TaskContext`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = ""
    repo: str = ""
    goal: str = ""
    scope: str = ""
    unified_diff: str = ""
    issue_text: str = ""
    from_version: str = ""
    to_version: str = ""
    model: str = ""
    locale: str = "zh-CN"
    max_turns: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_task_context(self) -> dict[str, Any]:
        """Materialise a :class:`~upgradelens.core.task.TaskContext` payload."""
        payload: dict[str, Any] = {
            "repo": self.repo,
            "goal": self.goal,
            "scope": self.scope,
            "unified_diff": self.unified_diff,
            "issue_text": self.issue_text,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "locale": self.locale,
            "options": self.extra,
        }
        if self.model:
            payload["model"] = self.model
        if self.max_turns:
            payload["max_turns"] = self.max_turns
        return payload


# ---------------------------------------------------------------------------
# Observability primitives
# ---------------------------------------------------------------------------


class LifecycleEvent(BaseModel):
    """One event in a run's lifecycle (start, step, checkpoint, finish, error)."""

    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    event: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    parent_run_id: RunId | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Checkpoint(BaseModel):
    """A resumable snapshot of a run's state."""

    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    step: str
    state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state_hash: str = ""

    def is_resumable(self) -> bool:
        return bool(self.state)


class TraceNode(BaseModel):
    """A node in the run trace tree (supervisor -> children)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    parent_id: str | None = None
    agent_id: str = ""
    kind: str = ""
    label: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.PENDING
    cost: CostUsage = Field(default_factory=CostUsage)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified result
# ---------------------------------------------------------------------------


class AgentResult(BaseModel):
    """The capability-agnostic result every agent returns.

    Field names deliberately mirror
    :class:`~upgradelens.capabilities.workbench.CapabilityRunResult` so the
    workbench can render any agent output through one template.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    parent_run_id: RunId | None = None
    agent_id: str
    kind: AgentKind = AgentKind.GENERIC
    status: RunStatus = RunStatus.COMPLETED
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    action_proposals: list[dict[str, Any]] = Field(default_factory=list)
    verification: VerificationResult | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    test_results: list[dict[str, Any]] = Field(default_factory=list)
    patch: str | None = None
    degradations: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    cost: CostUsage = Field(default_factory=CostUsage)
    notes: dict[str, Any] = Field(default_factory=dict)

    # -- convenience ------------------------------------------------------ #

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    @property
    def high_severity_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.HIGH)

    def to_capability_result(self) -> dict[str, Any]:
        """Bridge to the existing workbench :class:`CapabilityRunResult` shape."""
        return {
            "capability": self.kind.value,
            "summary": self.summary,
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "action_proposals": self.action_proposals,
            "verification": self.verification.model_dump(mode="json")
            if self.verification is not None
            else None,
            "coverage": self.coverage,
            "test_results": self.test_results,
            "patch": self.patch,
            "degradations": self.degradations,
            "trace": self.trace,
            "cost": self.cost.model_dump(mode="json"),
            "notes": self.notes,
        }


__all__ = [
    "RunId",
    "new_run_id",
    "AgentKind",
    "RunStatus",
    "AgentIdentity",
    "CostUsage",
    "DEFAULT_PERMISSIONS",
    "AgentRunContext",
    "LifecycleEvent",
    "Checkpoint",
    "TraceNode",
    "AgentResult",
    "TaskEnvelope",
]
