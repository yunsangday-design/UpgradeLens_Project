"""Render a security review result to human-readable markdown (plan stage S7)."""

from __future__ import annotations

from .analyzers import SecurityReviewResult

__all__ = ["render_security_review"]


def render_security_review(result: SecurityReviewResult) -> str:
    """Render the offline security review result as a markdown report."""
    report = result.report
    lines: list[str] = []
    title = report.review_id
    lines.append(f"# Security Review: {title}")
    lines.append("")
    files = ", ".join(c.path for c in result.change_set.files) or "(none)"
    lines.append(f"- Changed files: {files}")
    langs = ", ".join(lang.language for lang in result.profile.languages) or "(none)"
    lines.append(f"- Repository languages: {langs}")
    lines.append(f"- Verification: {'PASS' if result.gate.passed else 'FAIL'}")
    lines.append(f"- Coverage: {result.coverage.coverage:.0%}")
    lines.append("")

    findings = report.findings
    lines.append("## Security findings")
    lines.append("")
    if not findings:
        lines.append("No security findings.")
    for finding in findings:
        loc = f"{finding.file_path}:{finding.line}" if finding.line else finding.file_path
        flag = " (false positive)" if finding.false_positive else ""
        lines.append(
            f"- **[{finding.category.value}] {finding.severity}{flag}** "
            f"({finding.status}) `{loc}` -- {finding.title}"
        )
        if finding.description:
            lines.append(f"  - {finding.description}")
        if finding.recommendation:
            lines.append(f"  - Fix: {finding.recommendation}")
        cwe = finding.cwe if finding.cwe != "unknown" else None
        if cwe:
            lines.append(f"  - CWE: {cwe}")
    return "\n".join(lines)
