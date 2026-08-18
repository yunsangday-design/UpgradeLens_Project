"""Render a PR review result to human-readable markdown (plan stage S4)."""

from __future__ import annotations

from .analyzers import PRReviewResult

__all__ = ["render_pr_review"]


def render_pr_review(result: PRReviewResult) -> str:
    """Render the offline review result as a markdown report."""
    report = result.report
    lines: list[str] = []
    title = report.pr_title or report.review_id
    lines.append(f"# PR Review: {title}")
    lines.append("")
    files = ", ".join(c.path for c in result.change_set.files) or "(none)"
    lines.append(f"- Changed files: {files}")
    impacted = ", ".join(s.name for s in result.impact.impacted) or "(none)"
    lines.append(f"- Impacted symbols: {impacted}")
    lines.append(f"- Verification: {'PASS' if result.verification.passed else 'FAIL'}")
    lines.append("")

    if report.comments:
        lines.append("## Findings")
        lines.append("")
        for comment in report.comments:
            loc = f"{comment.file_path}:{comment.line}" if comment.line else comment.file_path
            lines.append(
                f"- **[{comment.category.value}] {comment.severity}** "
                f"({comment.status}) `{loc}` -- {comment.summary}"
            )
            if comment.recommendation:
                lines.append(f"  - Fix: {comment.recommendation}")
    else:
        lines.append("## Findings")
        lines.append("")
        lines.append("No review comments.")

    if result.tests:
        lines.append("")
        lines.append("## Recommended tests")
        lines.append("")
        for test in result.tests:
            paths = ", ".join(test.test_paths)
            lines.append(f"- {test.title}: `{paths}`")

    return "\n".join(lines)
