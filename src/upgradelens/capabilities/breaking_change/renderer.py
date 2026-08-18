"""Render a breaking-change result to human-readable markdown (plan stage S5)."""

from __future__ import annotations

from .analyzers import BreakingChangeResult

__all__ = ["render_breaking_change"]


def render_breaking_change(result: BreakingChangeResult) -> str:
    """Render the offline breaking-change result as a markdown report."""
    report = result.report
    lines: list[str] = []
    title = f"{report.from_version} -> {report.to_version}"
    lines.append(f"# Breaking Changes: {title}")
    lines.append("")
    lines.append(f"- Upgrade magnitude: {result.comparison.level}")
    changed = ", ".join(c.path for c in result.change_set.files) or "(none)"
    lines.append(f"- Changed files: {changed}")
    lines.append(f"- Verification: {'PASS' if result.verification.passed else 'FAIL'}")
    lines.append("")
    if report.changes:
        lines.append("## Changes")
        lines.append("")
        for change in report.changes:
            loc = f"{change.symbol}" if change.symbol else ""
            lines.append(
                f"- **[{change.kind.value}] {change.severity}** "
                f"({change.status}) `{loc}` -- {change.summary}"
            )
            if change.recommendation:
                lines.append(f"  - Fix: {change.recommendation}")
    else:
        lines.append("## Changes")
        lines.append("")
        lines.append("No breaking changes detected.")
    return "\n".join(lines)
