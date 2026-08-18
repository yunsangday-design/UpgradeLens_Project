"""The capability protocol and registry (plan stage S1).

A :class:`TaskCapability` declares, for one :class:`~upgradelens.core.task.TaskKind`:

- which tools it is allowed to call (``allowed_tools``);
- how to build a deterministic plan template (``build_plan``);
- which extra verifiers it needs.

The registry answers the central safety question for the agent loop: *may this
capability invoke tool X?* A capability may only call tools it declared -- the
loop must refuse anything else. This keeps the "least privilege" boundary that
the existing upgrade pipeline already enforces via ``PlanMode``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.core.task import SoftwareTask

__all__ = [
    "TaskCapability",
    "CapabilityPlan",
    "CapabilityRegistry",
    "BaseCapability",
    "CoveragePolicy",
    "ToolPermissionError",
]


class ToolPermissionError(PermissionError):
    """Raised when a capability tries to call a tool it did not declare."""


class CapabilityPlan(BaseModel):
    """Deterministic plan template a capability declares for a task.

    ``steps`` are ordered tool/phase names (not LLM-ordered). The agent loop may
    still adapt at runtime, but the template is the contract the capability owns.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    capability_kind: str = ""
    steps: list[str] = Field(default_factory=list)
    note: str = ""


class CoveragePolicy(BaseModel):
    """Quality gate a capability promises before its findings may drive actions.

    ``min_coverage`` / ``min_confidence`` are the evidence bar; ``required_inputs``
    are the task-context keys that must be present; ``forbidden_auto_fix`` blocks
    any unattended remediation (the safe default for upgrade/security work).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_coverage: float = 0.6
    required_inputs: list[str] = Field(default_factory=list)
    min_confidence: float = 0.5
    forbidden_auto_fix: bool = True
    notes: str = ""


@runtime_checkable
class TaskCapability(Protocol):
    """A pluggable software-engineering task handler.

    Members are declared read-only so frozen capability dataclasses (which expose
    them as init-only/read-only attributes) are compatible with the protocol.
    """

    @property
    def kind(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def allowed_tools(self) -> tuple[str, ...]: ...

    def build_plan(self, task: SoftwareTask) -> CapabilityPlan:
        """Return the deterministic plan template for ``task``."""
        ...

    def extra_verifier_names(self) -> tuple[str, ...]:
        """Optional extra verifier identifiers this capability needs."""
        ...


@dataclass(frozen=True)
class BaseCapability:
    """Minimal concrete base for capabilities that only declare metadata.

    Subclasses override ``build_plan`` and ``extra_verifier_names``; ``allowed_tools``
    is the security-relevant field the registry enforces. ``coverage_policy`` and
    ``verifier_names`` capture the quality gate and extra verifiers a capability needs.
    """

    kind: str = ""
    name: str = ""
    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    verifier_names: tuple[str, ...] = ()
    coverage_policy: CoveragePolicy = field(default_factory=CoveragePolicy)
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def build_plan(self, task: SoftwareTask) -> CapabilityPlan:
        return CapabilityPlan(
            task_id=task.task_id,
            capability_kind=self.kind,
            steps=list(self.allowed_tools),
        )

    def extra_verifier_names(self) -> tuple[str, ...]:
        return self.verifier_names


class CapabilityRegistry:
    """Holds the capabilities available to the runtime, keyed by task kind."""

    def __init__(self) -> None:
        self._caps: dict[str, TaskCapability] = {}

    def register(self, cap: TaskCapability) -> None:
        if not cap.kind:
            raise ValueError("capability.kind must not be empty")
        self._caps[cap.kind] = cap

    def get(self, kind: str) -> TaskCapability | None:
        return self._caps.get(kind)

    def all(self) -> list[TaskCapability]:
        return list(self._caps.values())

    def catalog(self) -> list[dict[str, Any]]:
        """JSON-serialisable description of every registered capability."""
        return [
            {
                "kind": c.kind,
                "name": c.name,
                "description": c.description,
                "allowed_tools": list(c.allowed_tools),
            }
            for c in self._caps.values()
        ]

    def allow_tool(self, kind: str, tool: str) -> bool:
        """Whether capability ``kind`` declared ``tool``."""
        cap = self.get(kind)
        if cap is None:
            return False
        return tool in cap.allowed_tools

    def require_tool(self, kind: str, tool: str) -> None:
        """Raise :class:`ToolPermissionError` if ``kind`` may not call ``tool``."""
        cap = self.get(kind)
        if cap is None or tool not in cap.allowed_tools:
            allowed = list(cap.allowed_tools) if cap is not None else []
            raise ToolPermissionError(
                f"capability {kind!r} is not allowed to call tool {tool!r}; "
                f"allowed tools: {allowed}"
            )
