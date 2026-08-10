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

from typing import Any

from pydantic import BaseModel, Field

# Step lifecycle. A plan is fully resolved when every step is in a terminal
# state (succeeded/failed/skipped); only ``pending``/``running`` steps are
# eligible to be picked by the loop.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"

TERMINAL_STATUSES = (SUCCEEDED, FAILED, SKIPPED)


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
    notes: list[str] = Field(default_factory=list)

    def pending(self) -> list[AgentPlanStep]:
        return [s for s in self.steps if s.status in (PENDING, RUNNING)]

    def is_resolved(self) -> bool:
        return all(s.status in TERMINAL_STATUSES for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentPlan:
        return cls.model_validate(data)
