"""Offline evaluation runner (plan section 18).

Runs every case against every baseline in a throwaway SQLite database and
returns both the per-case scores and the aggregated comparison. No network, no
model API, no user interaction -- the whole thing is reproducible on a laptop
with the repository alone.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.eval.baselines import BASELINES, build_artifacts, run_baseline
from upgradelens.eval.cases import EvalCase, load_cases
from upgradelens.eval.scorer import BaselineSummary, CaseScore, score_case, summarise

__all__ = ["EvaluationResult", "run_evaluation", "render_summary_markdown"]


@dataclass(frozen=True)
class EvaluationResult:
    """Everything one evaluation run produced."""

    scores: list[CaseScore]
    summaries: list[BaselineSummary]
    case_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "eval-result/1",
            "cases": self.case_ids,
            "baselines": [
                {
                    "baseline": s.baseline,
                    "cases": s.cases,
                    "passed_cases": s.passed_cases,
                    "pass_rate": round(s.pass_rate, 4),
                    "citation_existence_rate": round(s.citation_existence_rate, 4),
                    "hallucinated_verified": s.hallucinated_verified,
                    "failed_checks": s.failed_checks,
                }
                for s in self.summaries
            ],
            "details": [
                {
                    "case_id": s.case_id,
                    "baseline": s.baseline,
                    "passed": s.passed,
                    "checks": s.checks,
                    "citation_existence_rate": round(s.citation_existence_rate, 4),
                    "hallucinated_verified": s.hallucinated_verified,
                    "verified_risks": s.verified_count,
                    "degraded_risks": s.degraded_count,
                }
                for s in self.scores
            ],
        }


def _run_case(case: EvalCase, baselines: list[str], db_dir: Path) -> list[CaseScore]:
    """Evaluate one case across the requested baselines."""
    session = None
    if case.with_docs:
        engine = engine_for(db_dir / f"{case.case_id}.db")
        init_db(engine)
        session = session_for(engine)()
    try:
        artifacts = build_artifacts(case, session)
        return [
            score_case(case, name, run_baseline(name, artifacts), artifacts.bundle)
            for name in baselines
        ]
    finally:
        if session is not None:
            session.close()


def run_evaluation(
    cases_dir: Path,
    *,
    baselines: list[str] | None = None,
) -> EvaluationResult:
    """Run the full offline evaluation."""
    selected = baselines or sorted(BASELINES)
    unknown = [name for name in selected if name not in BASELINES]
    if unknown:
        raise ValueError(f"unknown baseline(s): {unknown} (known: {sorted(BASELINES)})")

    cases = load_cases(Path(cases_dir))
    scores: list[CaseScore] = []
    with tempfile.TemporaryDirectory(prefix="upgradelens-eval-") as tmp:
        db_dir = Path(tmp)
        for case in cases:
            scores.extend(_run_case(case, selected, db_dir))

    return EvaluationResult(
        scores=scores,
        summaries=summarise(scores),
        case_ids=[c.case_id for c in cases],
    )


def render_summary_markdown(result: EvaluationResult) -> str:
    """Render the baseline comparison as a Markdown table."""
    lines = [
        "# UpgradeLens offline evaluation",
        "",
        f"Cases: {len(result.case_ids)} — " + ", ".join(f"`{c}`" for c in result.case_ids),
        "",
        "## Baseline comparison",
        "",
        "| Baseline | Pass rate | Citation existence | Hallucinated as verified |",
        "| --- | ---: | ---: | ---: |",
    ]
    for summary in result.summaries:
        lines.append(
            f"| `{summary.baseline}` "
            f"| {summary.passed_cases}/{summary.cases} ({summary.pass_rate:.0%}) "
            f"| {summary.citation_existence_rate:.1%} "
            f"| {summary.hallucinated_verified} |"
        )
    lines.append("")

    lines += ["## Failed checks by baseline", ""]
    for summary in result.summaries:
        if summary.failed_checks:
            detail = ", ".join(f"`{k}` ×{v}" for k, v in summary.failed_checks.items())
        else:
            detail = "_none_"
        lines.append(f"- **{summary.baseline}**: {detail}")
    lines.append("")

    lines += [
        "## Per-case detail",
        "",
        "| Case | Baseline | Passed | Verified | Degraded | Citation existence |",
        "| --- | --- | :---: | ---: | ---: | ---: |",
    ]
    for score in result.scores:
        lines.append(
            f"| `{score.case_id}` | `{score.baseline}` "
            f"| {'yes' if score.passed else 'NO'} "
            f"| {score.verified_count} | {score.degraded_count} "
            f"| {score.citation_existence_rate:.0%} |"
        )
    return "\n".join(lines).rstrip() + "\n"
