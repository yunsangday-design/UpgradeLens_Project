"""Offline evaluation runner (plan section 18).

Runs every case against every baseline in a throwaway SQLite database and
returns both the per-case scores and the aggregated comparison. No network, no
model API, no user interaction -- the whole thing is reproducible on a laptop
with the repository alone.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.eval.baselines import BASELINES, build_artifacts, run_baseline
from upgradelens.eval.cases import EvalCase, load_cases
from upgradelens.eval.scorer import BaselineSummary, CaseScore, score_case, summarise
from upgradelens.llm.prompts import PROMPTS

__all__ = [
    "BaselineDelta",
    "ComparisonResult",
    "EvaluationResult",
    "compare_runs",
    "render_compare_markdown",
    "render_summary_markdown",
    "run_evaluation",
]


@dataclass(frozen=True)
class EvaluationResult:
    """Everything one evaluation run produced."""

    scores: list[CaseScore]
    summaries: list[BaselineSummary]
    case_ids: list[str]
    # Prompt template versions in effect when the run was made. This is the
    # lever for the A/B loop: a re-run after a prompt edit carries a different
    # ``prompt_versions`` value, so two ``to_dict`` outputs can be diffed and
    # the metric delta attributed to the prompt change rather than the fixtures.
    prompt_versions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationResult:
        """Rebuild a result from a previous ``to_dict`` for A/B comparison.

        Only the aggregated baseline summaries and the prompt versions are kept;
        the per-case rows are not needed to diff headline metrics.
        """
        summaries = [
            BaselineSummary(
                baseline=b["baseline"],
                cases=b["cases"],
                passed_cases=b["passed_cases"],
                cited_total=b.get("cited_total", 0),
                cited_existing=b.get("cited_existing", 0),
                hallucinated_verified=b.get("hallucinated_verified", 0),
                failed_checks=dict(b.get("failed_checks", {})),
            )
            for b in data.get("baselines", [])
        ]
        return cls(
            scores=[],
            summaries=summaries,
            case_ids=list(data.get("cases", [])),
            prompt_versions=dict(data.get("prompt_versions", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "eval-result/1",
            "prompt_versions": dict(self.prompt_versions),
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

    # Snapshot which prompt template versions produced this run. The eval
    # harness is offline (synthetic model reports), so the versions do not
    # change the output here -- they label it, which is what makes the A/B
    # loop (change prompt -> eval -> compare -> iterate) possible.
    prompt_versions = {name: template.version for name, template in PROMPTS.items()}

    return EvaluationResult(
        scores=scores,
        summaries=summarise(scores),
        case_ids=[c.case_id for c in cases],
        prompt_versions=prompt_versions,
    )


def render_summary_markdown(
    result: EvaluationResult, comparison: ComparisonResult | None = None
) -> str:
    """Render the baseline comparison as a Markdown table.

    The prompt versions are shown up front so a re-run can be attributed to a
    prompt change at a glance; when ``comparison`` is supplied the A/B delta is
    appended after the baseline table.
    """
    version_bits = (
        ", ".join(f"{k}={v}" for k, v in sorted(result.prompt_versions.items())) or "_none_"
    )
    lines = [
        "# UpgradeLens offline evaluation",
        "",
        f"Cases: {len(result.case_ids)} — " + ", ".join(f"`{c}`" for c in result.case_ids),
        "",
        f"Prompt versions: {version_bits}",
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

    if comparison is not None:
        lines.append("")
        lines.append(render_compare_markdown(comparison).rstrip())

    return "\n".join(lines).rstrip() + "\n"


# --- A/B comparison -------------------------------------------------------
#
# The point of recording ``prompt_versions`` on every run is to diff two runs
# after a prompt edit. ``compare_runs`` takes the current result and a previous
# ``to_dict`` blob, lines up each baseline, and judges whether the change moved
# the headline metrics the right way. The judgement is deliberately strict about
# hallucinations: an increase can never be an "improvement".


@dataclass(frozen=True)
class BaselineDelta:
    """Per-baseline movement between two evaluation runs."""

    baseline: str
    prev_pass_rate: float
    curr_pass_rate: float
    prev_citation_rate: float
    curr_citation_rate: float
    prev_hallucinated: int
    curr_hallucinated: int
    verdict: str  # "improved" | "regressed" | "mixed" | "unchanged"


@dataclass(frozen=True)
class ComparisonResult:
    """The full A/B comparison between two evaluation runs."""

    current_versions: dict[str, str]
    previous_versions: dict[str, str]
    deltas: tuple[BaselineDelta, ...]

    def overall_verdict(self) -> str:
        verdicts = {d.verdict for d in self.deltas}
        if "regressed" in verdicts:
            return "regressed"
        if "mixed" in verdicts:
            return "mixed"
        if verdicts == {"improved"}:
            return "improved"
        return "unchanged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_versions": dict(self.current_versions),
            "previous_versions": dict(self.previous_versions),
            "overall_verdict": self.overall_verdict(),
            "deltas": [
                {
                    "baseline": d.baseline,
                    "prev_pass_rate": d.prev_pass_rate,
                    "curr_pass_rate": d.curr_pass_rate,
                    "prev_citation_rate": d.prev_citation_rate,
                    "curr_citation_rate": d.curr_citation_rate,
                    "prev_hallucinated": d.prev_hallucinated,
                    "curr_hallucinated": d.curr_hallucinated,
                    "verdict": d.verdict,
                }
                for d in self.deltas
            ],
        }


def _round4(value: float) -> float:
    return round(float(value), 4)


def compare_runs(current: EvaluationResult, previous: dict[str, Any]) -> ComparisonResult:
    """Diff ``current`` against a prior ``to_dict`` blob, baseline by baseline."""
    prior = EvaluationResult.from_dict(previous)
    prior_by_baseline = {s.baseline: s for s in prior.summaries}

    deltas: list[BaselineDelta] = []
    for summary in current.summaries:
        before = prior_by_baseline.get(summary.baseline)
        if before is None:
            continue
        prev_pass = _round4(before.pass_rate)
        curr_pass = _round4(summary.pass_rate)
        prev_cit = _round4(before.citation_existence_rate)
        curr_cit = _round4(summary.citation_existence_rate)
        prev_hall = before.hallucinated_verified
        curr_hall = summary.hallucinated_verified

        regressed = (
            curr_pass < prev_pass - 1e-9 or curr_cit < prev_cit - 1e-9 or curr_hall > prev_hall
        )
        improved = (
            curr_pass > prev_pass + 1e-9 or curr_cit > prev_cit + 1e-9 or curr_hall < prev_hall
        )
        if regressed and improved:
            verdict = "mixed"
        elif regressed:
            verdict = "regressed"
        elif improved:
            verdict = "improved"
        else:
            verdict = "unchanged"

        deltas.append(
            BaselineDelta(
                baseline=summary.baseline,
                prev_pass_rate=prev_pass,
                curr_pass_rate=curr_pass,
                prev_citation_rate=prev_cit,
                curr_citation_rate=curr_cit,
                prev_hallucinated=prev_hall,
                curr_hallucinated=curr_hall,
                verdict=verdict,
            )
        )

    return ComparisonResult(
        current_versions=dict(current.prompt_versions),
        previous_versions=dict(prior.prompt_versions),
        deltas=tuple(deltas),
    )


def _fmt_rate(value: float) -> str:
    return f"{value:.0%}"


def _fmt_delta(prev: float, curr: float, fmt: str) -> str:
    arrow = "=" if abs(curr - prev) < 1e-9 else ("↑" if curr > prev else "↓")
    return f"{fmt.format(prev)}→{fmt.format(curr)} {arrow}"


def render_compare_markdown(comparison: ComparisonResult) -> str:
    """Render the A/B delta as a Markdown table plus an overall verdict."""
    prev_v = (
        ", ".join(f"{k}={v}" for k, v in sorted(comparison.previous_versions.items())) or "_none_"
    )
    curr_v = (
        ", ".join(f"{k}={v}" for k, v in sorted(comparison.current_versions.items())) or "_none_"
    )
    lines = [
        "## Prompt A/B comparison",
        "",
        f"Prompt versions — previous: {prev_v}",
        f"Prompt versions — current: {curr_v}",
        "",
        "| Baseline | Pass rate (prev→curr) | Citation (prev→curr) "
        "| Hallucinated (prev→curr) | Verdict |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for delta in comparison.deltas:
        hall_arrow = (
            "↑"
            if delta.curr_hallucinated > delta.prev_hallucinated
            else "↓"
            if delta.curr_hallucinated < delta.prev_hallucinated
            else "="
        )
        lines.append(
            f"| `{delta.baseline}` "
            f"| {_fmt_delta(delta.prev_pass_rate, delta.curr_pass_rate, '{:.0%}')} "
            f"| {_fmt_delta(delta.prev_citation_rate, delta.curr_citation_rate, '{:.1%}')} "
            f"| {delta.prev_hallucinated}→{delta.curr_hallucinated} {hall_arrow} "
            f"| {delta.verdict} |"
        )
    lines.append("")
    lines.append(f"**Overall verdict:** {comparison.overall_verdict()}")
    lines.append("")
    return "\n".join(lines)
