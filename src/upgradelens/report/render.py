"""Markdown rendering for :class:`~upgradelens.verify.models.VerifiedReport`.

The layout enforces the plan's core presentation rule: confirmed findings and
unproven findings live in separate sections that can never be confused, and
every severity is accompanied by the factors that produced it.

Rendering is pure and deterministic -- the same report always yields the same
bytes, which keeps documentation snapshots diffable.
"""

from __future__ import annotations

from upgradelens.verify.models import Conclusion, VerifiedReport, VerifiedRisk

__all__ = ["render_markdown"]

_CONCLUSION_TEXT = {
    Conclusion.IMPACTED: "Impacted — verified risks found",
    Conclusion.NO_IMPACT: "No impact — no production usage of this dependency was found",
    Conclusion.EVIDENCE_INSUFFICIENT: "Evidence insufficient — no risk could be fully verified",
}


def _conclusion_text(report: VerifiedReport) -> str:
    """Phrase the conclusion precisely.

    "No impact" covers two very different situations: the dependency is not
    used at all, or it *is* used but nothing matched a breaking change. Saying
    "no usage found" in the second case would be plainly wrong.
    """
    if report.conclusion is Conclusion.NO_IMPACT and report.evidence_summary.get("code_usage", 0):
        return "No impact — the dependency is used, but no breaking change matched the evidence"
    return _CONCLUSION_TEXT.get(report.conclusion, str(report.conclusion))


_STATUS_TEXT = {
    "verified": "Verified",
    "partially_verified": "Partially verified",
    "insufficient_evidence": "Insufficient evidence",
    "conflicting_evidence": "Conflicting evidence",
    "not_applicable": "Not applicable",
}


def _risk_block(risk: VerifiedRisk, *, index: int) -> list[str]:
    """Render one risk, including why it got its severity."""
    lines = [
        f"#### {index}. {risk.title or risk.risk_id}",
        "",
        f"- **Severity (rule-based):** `{risk.severity}`  ",
        f"- **Model severity:** `{risk.model_severity}`  ",
        f"- **Evidence status:** {_STATUS_TEXT.get(str(risk.status), str(risk.status))}  ",
        f"- **Rule score:** {risk.rule_score}",
        "",
    ]

    if risk.recommendation:
        lines += [f"> {risk.recommendation}", ""]

    if risk.code_evidence_ids:
        lines.append("**Code evidence**")
        lines += [f"- `{eid}`" for eid in risk.code_evidence_ids]
        lines.append("")
    if risk.doc_evidence_ids:
        lines.append("**Documentation evidence**")
        lines += [f"- `{eid}`" for eid in risk.doc_evidence_ids]
        lines.append("")

    contributing = [f for f in risk.factors if f.points != 0]
    if contributing:
        lines += [
            "**Why this severity**",
            "",
            "| Factor | Value | Points |",
            "| --- | --- | ---: |",
        ]
        lines += [f"| {f.name} | {f.value} | {f.points:+d} |" for f in contributing]
        lines.append("")

    if risk.issues:
        lines.append("**Open verification issues**")
        for issue in risk.issues:
            suffix = f" (`{issue.evidence_id}`)" if issue.evidence_id else ""
            lines.append(f"- `{issue.code}` — {issue.detail}{suffix}")
        lines.append("")

    if risk.recommended_tests:
        lines.append("**Tests that likely cover this**")
        lines += [
            f"- `{t.test_path}` → `{t.production_path}` ({t.matched_by})"
            for t in risk.recommended_tests
        ]
        lines.append("")

    return lines


def render_markdown(report: VerifiedReport, max_chars: int | None = None) -> str:
    """Render ``report`` as a Markdown document.

    If ``max_chars`` is provided and the document is longer, the body is cut at
    the nearest line boundary and a short note is appended. This keeps the
    output within platform comment length caps (e.g. GitHub PR comments).
    """
    dependency = report.target_dependency or "(unknown dependency)"
    lines: list[str] = [
        f"# Upgrade impact report — {dependency}",
        "",
        f"- **Target version:** `{report.target_version_spec or 'n/a'}`",
        f"- **Declared version:** `{report.source_version_spec or 'n/a'}`",
        f"- **Version source:** {report.source_version_source or 'n/a'}",
        f"- **Generated:** {report.generated_at}",
        f"- **Analysis mode:** {'static fallback' if report.static else 'model-assisted'}",
        "",
        "## Conclusion",
        "",
        f"**{_conclusion_text(report)}**",
        "",
    ]

    if report.partial:
        lines += ["> **This is a partial report.** Coverage is incomplete:", ""]
        lines += [f"> - {note}" for note in report.degradations]
        lines.append("")

    lines += [
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Verified risks | {len(report.verified_risks)} |",
        f"| Unverified findings | {len(report.degraded_risks)} |",
        f"| Recommended tests | {len(report.recommended_tests)} |",
        f"| Citation existence rate | {report.citation_existence_rate:.0%} |",
    ]
    for key in sorted(report.evidence_summary):
        lines.append(f"| Evidence: {key} | {report.evidence_summary[key]} |")
    lines.append("")

    lines += ["## Verified risks", ""]
    if report.verified_risks:
        for index, risk in enumerate(report.verified_risks, start=1):
            lines += _risk_block(risk, index=index)
    else:
        lines += ["_No risk passed full verification._", ""]

    if report.degraded_risks:
        lines += [
            "## Unverified findings",
            "",
            (
                "_These did **not** pass verification. They are shown for triage only "
                "and must not be treated as confirmed._"
            ),
            "",
        ]
        for index, risk in enumerate(report.degraded_risks, start=1):
            lines += _risk_block(risk, index=index)

    lines += ["## Recommended tests", ""]
    if report.recommended_tests:
        lines += ["| Test | Covers | Matched by |", "| --- | --- | --- |"]
        lines += [
            f"| `{t.test_path}` | `{t.production_path}` | {t.matched_by} |"
            for t in report.recommended_tests
        ]
        lines.append("")
    else:
        lines += ["_No existing test was found for the impacted modules._", ""]

    if report.notes:
        lines += ["## Notes", "", report.notes, ""]

    text = "\n".join(lines).rstrip() + "\n"
    if max_chars is None or len(text) <= max_chars:
        return text

    # Truncate at the last line boundary that keeps us under the cap, then add
    # a short notice. If even one line is too long, hard-cut to stay safe.
    truncated: list[str] = []
    used = 0
    notice = "\n\n<!-- UpgradeLens: report truncated to fit the platform limit. -->\n"
    for line in lines:
        addition = len(line) + 1
        if used + addition + len(notice) > max_chars and truncated:
            break
        truncated.append(line)
        used += addition
    return "\n".join(truncated).rstrip() + notice
