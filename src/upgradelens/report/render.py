"""Markdown rendering for :class:`~upgradelens.verify.models.VerifiedReport`.

The layout enforces the plan's core presentation rule: confirmed findings and
unproven findings live in separate sections that can never be confused, and
every severity is accompanied by the factors that produced it.

Rendering is pure and deterministic -- the same report always yields the same
bytes, which keeps documentation snapshots diffable.
"""

from __future__ import annotations

from upgradelens.plan.upgrade_plan import UpgradePlan
from upgradelens.verify.models import Conclusion, VerifiedReport, VerifiedRisk

__all__ = ["render_markdown", "render_plan_markdown"]

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


def render_plan_markdown(plan: UpgradePlan) -> str:
    """Render a :class:`UpgradePlan` as a Chinese 修改说明.

    Pure and deterministic; the plan is a read-only projection so the same plan
    always yields the same bytes.
    """
    if plan is None:
        return ""
    lines: list[str] = [
        f"# 升级修改计划 — {plan.target_dependency}",
        "",
        f"- **目标版本:** `{plan.target_version_spec or 'n/a'}`",
        f"- **声明版本:** `{plan.source_version_spec or 'n/a'}`",
        f"- **仓库哈希:** `{plan.repo_hash or '<unknown>'}`",
        f"- **计划模式:** `{plan.mode.value if hasattr(plan.mode, 'value') else plan.mode}`",
        f"- **部署契约:** {'要求' if plan.deploy_contract else '不要求'}",
        f"- **步骤数:** {len(plan.steps)}",
        "",
    ]

    if plan.steps:
        for index, step in enumerate(plan.steps, start=1):
            sev = step.severity or "low"
            status = step.evidence_status or ""
            lines += [
                f"## 步骤 {index}: {step.title}  [{sev}/{status}]",
                "",
            ]
            if step.change_reason:
                lines += [f"**为什么改:** {step.change_reason}", ""]
            if step.target_files:
                lines += [
                    "**涉及文件:** " + ", ".join(f"`{f}`" for f in step.target_files),
                    "",
                ]
            if step.api_symbols:
                lines += [
                    "**相关 API:** " + ", ".join(f"`{s}`" for s in step.api_symbols),
                    "",
                ]
            if step.completion_criteria:
                lines += ["**完成标准:**"]
                lines += [f"- {c}" for c in step.completion_criteria]
                lines.append("")
            if step.before_example:
                lines += [
                    "**升级前:**",
                    "```",
                    step.before_example.rstrip(),
                    "```",
                    "",
                ]
            if step.after_example:
                lines += [
                    "**升级后:**",
                    "```",
                    step.after_example.rstrip(),
                    "```",
                    "",
                ]
    else:
        lines += ["_无需要修改的步骤。_", ""]

    if plan.patch and not plan.patch.is_empty:
        patch_text = plan.patch.to_unified_diff()
        if patch_text:
            lines += [
                "## 补丁草稿",
                "",
                "```diff",
                patch_text.rstrip(),
                "```",
                "",
            ]

    if plan.warnings:
        lines += ["## 警告", ""]
        lines += [f"- {w}" for w in plan.warnings]
        lines.append("")

    if plan.deploy_contract:
        lines += [
            "> **部署契约:** 本次升级包含需重新部署的破坏性变更，"
            "请在发布窗口内完成滚动发布并准备回滚预案。",
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"
