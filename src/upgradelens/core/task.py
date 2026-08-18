"""The unit of work a caller submits to UpgradeLens (plan stage S1).

A :class:`SoftwareTask` is deliberately capability-agnostic: it carries the task
kind, the natural-language goal, and an open ``context`` bag. Capabilities read
whatever fields they understand from ``context`` and ignore the rest. This keeps
a single entry point while allowing PR review, issue repair and security review
to evolve their inputs without a shared mega-model.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TaskKind", "TaskStatus", "TaskContext", "SoftwareTask"]


class TaskKind(StrEnum):
    """The high-level capability a task maps to.

    ``UNKNOWN`` is the routing fallback; a capability must never claim it.
    """

    DEPENDENCY_UPGRADE = "dependency_upgrade"
    PR_REVIEW = "pr_review"
    BREAKING_CHANGE = "breaking_change"
    ISSUE_REPAIR = "issue_repair"
    SECURITY_REVIEW = "security_review"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    """Lifecycle of a task run, mirrored from the agent loop's step states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"


class TaskContext(BaseModel):
    """Per-task structured inputs.

    Capability-specific fields live here so a new capability can add inputs without
    changing the :class:`SoftwareTask` schema. The dependency-upgrade keys are
    first-class (typed) so the router can populate them and mypy can see them; other
    keys may still be attached as ``extra`` fields.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    repo: str = ""
    dependency: str = ""
    source_version: str = ""
    target_version: str = ""
    # Capability-specific inputs (first-class so mypy can see them; they were
    # previously only accepted as ``extra`` fields). All optional with defaults.
    unified_diff: str = ""
    issue_text: str = ""
    from_version: str = ""
    to_version: str = ""
    # Other optional keys recognised by some capabilities (still extras):
    #   pr_number: int | str
    #   base_ref / head_ref: str
    #   issue_number: int | str


class SoftwareTask(BaseModel):
    """A single software-engineering task submitted to the runtime.

    Frozen and ``extra="forbid"`` so a task captured at intake is immutable for
    its whole lifecycle -- downstream steps annotate results, never the task.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    kind: TaskKind = TaskKind.UNKNOWN
    goal: str = ""
    context: TaskContext = Field(default_factory=TaskContext)
    locale: str = "zh-CN"

    def model_dump_context(self) -> dict[str, Any]:
        """Convenience accessor for the context bag."""
        return self.context.model_dump()
