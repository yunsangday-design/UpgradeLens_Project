"""Render an issue-repair result to human-readable markdown (plan stage S6)."""

from __future__ import annotations

from .analyzers import IssueRepairResult

__all__ = ["render_issue_repair"]


def render_issue_repair(result: IssueRepairResult) -> str:
    """Render the offline issue-repair result as a markdown report."""
    report = result.report
    lines: list[str] = []
    lines.append(f"# Issue Repair: {report.issue_id}")
    lines.append("")
    lines.append(f"- Root cause: {report.root_cause or '(none)'}")
    lines.append(f"- Verification: {'PASS' if result.verification.passed else 'FAIL'}")
    lines.append("")
    if result.actions:
        lines.append("## Proposed patch")
        lines.append("")
        for action in result.actions:
            targets = ", ".join(action.target_files) or "(none)"
            lines.append(f"- `{targets}` (kind={action.kind.value})")
    else:
        lines.append("## Proposed patch")
        lines.append("")
        lines.append("No patch proposed.")
    if report.suggested_tests:
        lines.append("")
        lines.append("## Suggested tests")
        lines.append("")
        for test in report.suggested_tests:
            lines.append(f"- {test}")
    return "\n".join(lines)
