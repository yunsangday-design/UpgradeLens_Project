"""ROADMAP Step 3 -- public plan models for an agent run.

An :class:`AgentPlan` is the live, written-down contract for one end-to-end
run: the ordered :class:`AgentPlanStep` objects the run will attempt, plus a
running status for each. The ReAct loop updates every step as it executes
(``pending`` -> ``running`` -> ``succeeded``/``failed``/``skipped``) and writes
the plan back atomically after each change, so a crash mid-run still leaves a
coherent, diffable artifact. Any tool call the model makes that is not already
in the plan is recorded as an ad-hoc step, so the plan always explains *why*
every tool ran.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from upgradelens.agent.coverage import CoverageSummary
from upgradelens.verify.models import VerificationIssue

# Step lifecycle. A plan is fully resolved when every step is in a terminal
# state (succeeded/failed/skipped); only ``pending``/``running`` steps are
# eligible to be picked by the loop.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"

TERMINAL_STATUSES = (SUCCEEDED, FAILED, SKIPPED)


class PlanStatus(StrEnum):
    """Terminal/lifecycle status for a whole run (ROADMAP Step 5)."""

    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_DEGRADATION = "completed_with_degradation"
    NEEDS_HUMAN = "needs_human"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class AgentPlanStep(BaseModel):
    """A single step in an :class:`AgentPlan`."""

    id: str = Field(default="", description="Stable step id, e.g. 's1' or 'a2'.")
    tool: str = Field(default="", description="Tool name this step invokes.")
    seq: int = Field(default=0, description="Position in the plan order.")
    status: str = Field(
        default=PENDING,
        description="pending|running|succeeded|failed|skipped",
    )
    phase: str = Field(default="collect", description="collect|analyse.")
    reason: str = Field(default="", description="Why this step is in the plan.")
    observation: str | None = Field(
        default=None, description="Short result summary after execution."
    )
    dependents: list[str] = Field(default_factory=list)
    retryable: bool = Field(default=True)
    attempt: int = Field(default=0, description="How many times this step ran.")
    evidence_ids: list[str] = Field(
        default_factory=list, description="Evidence ids produced by execution."
    )

    def mark_running(self) -> None:
        """Transition pending -> running and count the attempt."""
        self.status = RUNNING
        self.attempt += 1

    def mark_outcome(self, ok: bool, observation: str | None) -> None:
        """Transition running -> succeeded/failed and record the observation."""
        self.observation = observation
        self.status = SUCCEEDED if ok else FAILED

    def mark_skipped(self, observation: str | None = None) -> None:
        """Mark a step that can never run (e.g. no db for doc retrieval)."""
        self.observation = observation
        self.status = SKIPPED


class AgentPlan(BaseModel):
    """The live plan for one run, written atomically after every update."""

    request_id: str = Field(default="")
    mode: str = Field(default="")
    target_version_spec: str = Field(default="")
    source_version_spec: str = Field(default="")
    steps: list[AgentPlanStep] = Field(default_factory=list)
    degrade_to_pipeline: bool = Field(
        default=False, description="Driven loop could not collect; fell back."
    )
    coverage: CoverageSummary | None = Field(
        default=None,
        description="ROADMAP Step 4: doc-evidence coverage of code symbols + "
        "supplementary-retrieval summary, set after collection.",
    )
    notes: list[str] = Field(default_factory=list)
    status: str = Field(
        default=PlanStatus.RUNNING.value,
        description="lifecycle status: running|completed|completed_with_degradation|"
        "needs_human|budget_exhausted|failed (ROADMAP Step 5).",
    )
    unresolved_risks: list[VerificationIssue] = Field(
        default_factory=list,
        description="ROADMAP Step 5: verifier issues written back from the last "
        "verification round for traceability.",
    )
    replan_count: int = Field(
        default=0, description="How many verification/remediation rounds ran."
    )

    def pending(self) -> list[AgentPlanStep]:
        return [s for s in self.steps if s.status in (PENDING, RUNNING)]

    def is_resolved(self) -> bool:
        return all(s.status in TERMINAL_STATUSES for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentPlan:
        return cls.model_validate(data)
