"""Proposed actions: how the agent suggests fixing or testing (plan stage S1).

Every modification the agent proposes is an :class:`ActionProposal`. The base
class defaults ``requires_approval`` to ``True`` so that *every* write operation
is opt-in -- the system must be told it is allowed to act. Concrete subclasses
specialise the proposal for patches, tests, commands and manual steps.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActionKind",
    "ActionProposal",
    "PatchProposal",
    "TestProposal",
    "CommandProposal",
    "ManualAction",
]


class ActionKind(StrEnum):
    """What category of change a proposal represents."""

    PATCH = "patch"
    TEST = "test"
    COMMAND = "command"
    MANUAL = "manual"


class ActionProposal(BaseModel):
    """Base proposal. Subclasses narrow ``kind`` and add the relevant payload.

    ``requires_approval`` defaults to ``True``: no proposal is executed without
    an explicit go-ahead, matching the sandbox/approval boundary of the existing
    upgrade executor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    kind: ActionKind = ActionKind.MANUAL
    finding_ids: list[str] = Field(default_factory=list)
    title: str = ""
    description: str = ""
    requires_approval: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Keep pydantic models whose names look test-like out of pytest collection.
    __test__ = False


class PatchProposal(ActionProposal):
    """A unified-diff to apply (in the sandbox, never directly to the repo)."""

    kind: ActionKind = ActionKind.PATCH
    diff: str = ""
    target_files: list[str] = Field(default_factory=list)
    base_branch: str = ""


class TestProposal(ActionProposal):
    """A new or existing test to run for verification.

    ``intended_to_fail_before_fix`` marks a regression/repro test that should fail
    on the unpatched code and pass after the fix -- the strongest proof a fix works.
    """

    kind: ActionKind = ActionKind.TEST
    test_paths: list[str] = Field(default_factory=list)
    command: str = ""
    intended_to_fail_before_fix: bool = False


class CommandProposal(ActionProposal):
    """A shell command to run (e.g. lint, build, scan).

    ``allowed`` must be explicitly set by an allow-list check; a command proposal
    is never auto-approved just because it was generated.
    """

    kind: ActionKind = ActionKind.COMMAND
    command: str = ""
    allowed: bool = False


class ManualAction(ActionProposal):
    """A step the human must perform; the agent only describes it."""

    kind: ActionKind = ActionKind.MANUAL
    instructions: str = ""
