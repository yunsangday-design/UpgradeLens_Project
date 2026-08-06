"""Test candidate recommendation (plan section 6, "测试候选关联").

Only *real* test files are ever recommended. The link between a test file and
the production module it exercises comes from stage 2
(:class:`~upgradelens.domain.code_evidence.TestProductionLink`), and we re-check
that the file still exists on disk before surfacing it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.verify.models import TestCandidate

__all__ = ["recommend_tests", "rank_tests"]


def recommend_tests(
    code_report: CodeEvidenceReport,
    impacted_paths: Iterable[str],
    *,
    repo_root: Path,
) -> list[TestCandidate]:
    """Return the existing test files that cover ``impacted_paths``.

    The result is deterministic (sorted by test path) so reports and fixtures
    stay byte-stable across runs.
    """
    wanted = {p for p in impacted_paths if p}
    if not wanted:
        return []

    seen: set[tuple[str, str]] = set()
    out: list[TestCandidate] = []
    for link in code_report.test_production_links:
        if link.production_path not in wanted:
            continue
        key = (link.test_path, link.production_path)
        if key in seen:
            continue
        if not (repo_root / link.test_path).is_file():
            continue
        seen.add(key)
        out.append(
            TestCandidate(
                test_path=link.test_path,
                production_path=link.production_path,
                matched_by=link.matched_by,
                reason=f"covers impacted module {link.production_path}",
            )
        )
    return sorted(out, key=lambda c: (c.test_path, c.production_path))


def rank_tests(
    per_risk: dict[str, list[TestCandidate]],
    severities: dict[str, str],
) -> list[TestCandidate]:
    """Flatten per-risk candidates into one prioritised list.

    A test that covers more risks, and more severe ones, comes first. Ties are
    broken by path so the ordering never depends on dict iteration order.
    """
    weight = {"high": 3, "medium": 2, "low": 1}
    scores: dict[str, int] = {}
    best: dict[str, TestCandidate] = {}
    for risk_id, candidates in per_risk.items():
        points = weight.get(severities.get(risk_id, "low"), 1)
        for candidate in candidates:
            scores[candidate.test_path] = scores.get(candidate.test_path, 0) + points
            best.setdefault(candidate.test_path, candidate)
    ordered = sorted(best.values(), key=lambda c: (-scores[c.test_path], c.test_path))
    return ordered
