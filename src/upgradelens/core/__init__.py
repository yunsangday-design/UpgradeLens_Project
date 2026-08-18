"""Generic software-engineering task runtime (plan stage S1).

This package holds task-agnostic contracts that every capability -- dependency
upgrade, PR review, issue repair, security review -- shares:

- :mod:`~upgradelens.core.task` -- the ``SoftwareTask`` a user submits;
- :mod:`~upgradelens.core.finding` -- the evidence-backed ``Finding``;
- :mod:`~upgradelens.core.action` -- the ``ActionProposal`` family (patch/test/command/manual);
- :mod:`~upgradelens.core.verification` -- the ``VerificationResult`` of an action;
- :mod:`~upgradelens.core.capability` -- the ``TaskCapability`` protocol and registry.

The dependency-upgrade flow keeps working untouched; these models are additive
and become the shared language once capabilities are registered (S2+).
"""

from __future__ import annotations

from upgradelens.core.action import (
    ActionKind,
    ActionProposal,
    CommandProposal,
    ManualAction,
    PatchProposal,
    TestProposal,
)
from upgradelens.core.capability import (
    BaseCapability,
    CapabilityPlan,
    CapabilityRegistry,
    TaskCapability,
)
from upgradelens.core.finding import (
    EvidenceLink,
    Finding,
    FindingStatus,
    Severity,
)
from upgradelens.core.task import (
    SoftwareTask,
    TaskContext,
    TaskKind,
    TaskStatus,
)
from upgradelens.core.verification import (
    VerificationCheck,
    VerificationResult,
)

__all__ = [
    "SoftwareTask",
    "TaskContext",
    "TaskKind",
    "TaskStatus",
    "Finding",
    "FindingStatus",
    "EvidenceLink",
    "Severity",
    "ActionProposal",
    "ActionKind",
    "PatchProposal",
    "TestProposal",
    "CommandProposal",
    "ManualAction",
    "VerificationResult",
    "VerificationCheck",
    "TaskCapability",
    "CapabilityPlan",
    "CapabilityRegistry",
    "BaseCapability",
]
