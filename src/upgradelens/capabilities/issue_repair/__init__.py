"""Issue Repair capability package (plan stage S6)."""

from __future__ import annotations

from upgradelens.capabilities.issue_repair.analyzers import (
    IssueRepairResult,
    load_issue,
    locate_root_cause,
    repair_issue,
    report_to_findings,
)
from upgradelens.capabilities.issue_repair.capability import (
    IssueRepairCapability,
    build_issue_repair_plan,
    get_issue_repair_capability,
)
from upgradelens.capabilities.issue_repair.models import Issue, IssueRepairReport
from upgradelens.capabilities.issue_repair.renderer import render_issue_repair
from upgradelens.capabilities.issue_repair.tools import (
    ISSUE_REPAIR_TOOL_NAMES,
    issue_repair_tools,
)
from upgradelens.capabilities.issue_repair.verifiers import verify_issue_repair

__all__ = [
    "IssueRepairCapability",
    "IssueRepairReport",
    "IssueRepairResult",
    "ISSUE_REPAIR_TOOL_NAMES",
    "Issue",
    "build_issue_repair_plan",
    "get_issue_repair_capability",
    "issue_repair_tools",
    "load_issue",
    "locate_root_cause",
    "render_issue_repair",
    "repair_issue",
    "report_to_findings",
    "verify_issue_repair",
]
