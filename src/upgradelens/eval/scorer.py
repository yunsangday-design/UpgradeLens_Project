"""Scoring for the offline evaluation (plan section 18.4).

Every metric is recomputed from the evidence bundle rather than read off the
report. This matters: the ``llm_only`` baseline never populates
``unknown_evidence_ids``, so trusting a report's self-assessment would score a
hallucinating system as perfect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from upgradelens.eval.cases import EvalCase
from upgradelens.models.impact import EvidenceBundle
from upgradelens.verify.models import VerifiedReport, VerifiedRisk

__all__ = ["CaseScore", "BaselineSummary", "score_case", "summarise"]

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _all_cited(risk: VerifiedRisk) -> list[str]:
    return [*risk.code_evidence_ids, *risk.doc_evidence_ids, *risk.unknown_evidence_ids]


@dataclass(frozen=True)
class CaseScore:
    """Result of one (case, baseline) pair."""

    case_id: str
    baseline: str
    checks: dict[str, bool] = field(default_factory=dict)
    cited_total: int = 0
    cited_existing: int = 0
    hallucinated_verified: int = 0
    verified_count: int = 0
    degraded_count: int = 0

    @property
    def citation_existence_rate(self) -> float:
        if self.cited_total == 0:
            return 1.0
        return self.cited_existing / self.cited_total

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score_case(
    case: EvalCase,
    baseline: str,
    report: VerifiedReport,
    bundle: EvidenceBundle,
) -> CaseScore:
    """Score one report against the case's expectations."""
    expect = case.expect
    checks: dict[str, bool] = {}

    if expect.conclusion is not None:
        checks["conclusion"] = str(report.conclusion) == expect.conclusion

    verified_count = len(report.verified_risks)
    if expect.min_verified_risks is not None:
        checks["min_verified_risks"] = verified_count >= expect.min_verified_risks
    if expect.max_verified_risks is not None:
        checks["max_verified_risks"] = verified_count <= expect.max_verified_risks

    # Localisation is scored across *all* surfaced risks, not just the verified
    # ones: a case may legitimately expect a finding to be degraded (missing or
    # conflicting docs) while still demanding that the system located it.
    cited_paths: set[str] = set()
    flagged: set[str] = set()
    for risk in report.all_risks:
        for eid in risk.code_evidence_ids:
            item = bundle.get(eid)
            if item is None:
                continue
            cited_paths.add(str(item.meta.get("path", "")))
            flagged.add(str(item.meta.get("symbol", "")))

    if expect.must_cite_paths:
        checks["must_cite_paths"] = all(p in cited_paths for p in expect.must_cite_paths)
    if expect.must_flag_symbols:
        checks["must_flag_symbols"] = all(s in flagged for s in expect.must_flag_symbols)

    if expect.must_quarantine_risk_ids:
        verified_ids = {r.risk_id for r in report.verified_risks}
        checks["must_quarantine"] = all(
            rid not in verified_ids for rid in expect.must_quarantine_risk_ids
        )

    if expect.partial is not None:
        checks["partial"] = report.partial == expect.partial

    if expect.max_severity is not None:
        ceiling = _SEVERITY_ORDER.get(expect.max_severity, 0)
        checks["max_severity"] = all(
            _SEVERITY_ORDER.get(risk.severity, 0) <= ceiling for risk in report.verified_risks
        )

    cited_total = 0
    cited_existing = 0
    for risk in report.all_risks:
        for eid in _all_cited(risk):
            cited_total += 1
            if bundle.has(eid):
                cited_existing += 1

    hallucinated_verified = sum(
        1 for risk in report.verified_risks if any(not bundle.has(eid) for eid in _all_cited(risk))
    )
    # A system must never present an unverifiable claim as confirmed.
    checks["no_hallucinated_verified"] = hallucinated_verified == 0

    return CaseScore(
        case_id=case.case_id,
        baseline=baseline,
        checks=checks,
        cited_total=cited_total,
        cited_existing=cited_existing,
        hallucinated_verified=hallucinated_verified,
        verified_count=verified_count,
        degraded_count=len(report.degraded_risks),
    )


@dataclass(frozen=True)
class BaselineSummary:
    """Aggregated metrics for one baseline across all cases."""

    baseline: str
    cases: int
    passed_cases: int
    cited_total: int
    cited_existing: int
    hallucinated_verified: int
    failed_checks: dict[str, int] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passed_cases / self.cases if self.cases else 0.0

    @property
    def citation_existence_rate(self) -> float:
        if self.cited_total == 0:
            return 1.0
        return self.cited_existing / self.cited_total


def summarise(scores: list[CaseScore]) -> list[BaselineSummary]:
    """Aggregate per-case scores into one row per baseline."""
    by_baseline: dict[str, list[CaseScore]] = {}
    for score in scores:
        by_baseline.setdefault(score.baseline, []).append(score)

    out: list[BaselineSummary] = []
    for baseline in sorted(by_baseline):
        group = by_baseline[baseline]
        failed: dict[str, int] = {}
        for score in group:
            for name, ok in score.checks.items():
                if not ok:
                    failed[name] = failed.get(name, 0) + 1
        out.append(
            BaselineSummary(
                baseline=baseline,
                cases=len(group),
                passed_cases=sum(1 for s in group if s.passed),
                cited_total=sum(s.cited_total for s in group),
                cited_existing=sum(s.cited_existing for s in group),
                hallucinated_verified=sum(s.hallucinated_verified for s in group),
                failed_checks=dict(sorted(failed.items())),
            )
        )
    return out
