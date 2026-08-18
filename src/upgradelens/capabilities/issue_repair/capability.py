"""Issue Repair capability (plan stage S6).

Declares the tool surface an issue-repair run may use and the quality gate before
a patch can be proposed (it must target real files, and auto-apply is forbidden).
"""

from __future__ import annotations

from upgradelens.core.capability import BaseCapability, CapabilityPlan, CoveragePolicy
from upgradelens.core.task import SoftwareTask

from .tools import ISSUE_REPAIR_TOOL_NAMES

__all__ = ["IssueRepairCapability", "get_issue_repair_capability"]


class IssueRepairCapability(BaseCapability):
    """Propose a patch + tests for a reported issue, grounded in the repo."""


def get_issue_repair_capability() -> IssueRepairCapability:
    return IssueRepairCapability(
        kind="issue_repair",
        name="Issue Repair",
        description=(
            "Diagnose a reported issue, locate the root cause in the repository, and "
            "propose a verified patch with regression tests. Dangerous operations "
            "require explicit approval."
        ),
        allowed_tools=ISSUE_REPAIR_TOOL_NAMES,
        verifier_names=("issue_repair_verifier",),
        coverage_policy=CoveragePolicy(
            min_coverage=0.5,
            required_inputs=["repo_root", "issue_text"],
            min_confidence=0.5,
            forbidden_auto_fix=True,
            notes="Patches never auto-apply; every fix requires approval.",
        ),
    )


def build_issue_repair_plan(task: SoftwareTask) -> CapabilityPlan:
    """Return the deterministic issue-repair plan template for ``task``."""
    cap = get_issue_repair_capability()
    return CapabilityPlan(
        task_id=task.task_id,
        capability_kind=cap.kind,
        steps=list(cap.allowed_tools),
        note="Issue diagnosis & repair via deterministic analyzers and one fake-able model node.",
    )
