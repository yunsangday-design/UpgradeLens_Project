"""ROADMAP Step 4 -- evidence coverage + autonomous supplementary retrieval.

After the agent has retrieved documentation for a dependency (``retrieve_for_package``),
S4 checks whether the retrieved evidence actually *covers* the API symbols the
repository uses in code. Any symbol with no matching doc evidence is a *coverage
gap*; for each gap the loop performs a focused *supplementary retrieval* and
re-checks. If gaps remain after ``MAX_SUPPLEMENTARY`` attempts the run is flagged
degraded (evidence coverage insufficient) so a human can judge it (``needs_human``).

The detection is deliberately deterministic so the behaviour is identical in
``fake`` and ``live`` mode and fully testable offline. The only mode-dependent
piece is *how* the supplementary query is phrased: ``fake`` uses a template,
``live`` asks the LLM to rewrite a focused query (see ``agent.loop``).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeUsage
from upgradelens.domain.doc_evidence import RetrievalRun

# A symbol is "covered" when its identifier appears in a doc-evidence blob after
# normalising *only* the intra-identifier naming style (snake_case / kebab-case /
# CamelCase), never the surrounding prose. See step12-A.
_IDENT_EDGE = r"(?<![a-z0-9])"
_IDENT_EDGE_END = r"(?![a-z0-9])"


def _symbol_components(symbol: str) -> list[str]:
    """Split a code symbol into its naming-style components on ``_`` / ``-``."""
    return [p for p in re.split(r"[_\-]+", symbol) if p]


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    r"""Case-insensitive regex matching ``symbol`` across naming styles.

    Components are joined by ``[_\-\\s]*`` so ``declarative_base`` also matches
    ``DeclarativeBase`` / ``declarative-base`` / ``declarative base``, while never
    matching when unrelated words sit between the components (e.g. "declarative
    foo bar base"). Plain symbols (no ``_``/``-``) keep an identifier-bounded
    substring match.
    """
    components = _symbol_components(symbol)
    body = (
        r"[_\-\s]*".join(re.escape(c) for c in components)
        if len(components) > 1
        else re.escape(symbol)
    )
    return re.compile(_IDENT_EDGE + body + _IDENT_EDGE_END, re.IGNORECASE)


def _is_symbol_covered(symbol: str, blobs: list[str]) -> bool:
    rx = _symbol_pattern(symbol)
    return any(rx.search(blob) for blob in blobs)


class CoverageGap(BaseModel):
    """One code symbol with no matching documentation evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(description="Code symbol that has no matching doc evidence.")
    usage_count: int = Field(description="How many code usages reference the symbol.")
    sample_paths: list[str] = Field(default_factory=list)
    reason: str = Field(default="", description="Why the symbol is considered uncovered.")


class CoverageResult(BaseModel):
    """Deterministic coverage assessment of code symbols against doc evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_symbols: int
    covered_symbols: int
    uncovered_symbols: int
    coverage_rate: float
    gaps: list[CoverageGap] = Field(default_factory=list)


class CoverageSummary(BaseModel):
    """Lightweight, JSON-serialisable coverage snapshot stored on the plan."""

    model_config = ConfigDict(extra="forbid")

    total_symbols: int = 0
    covered_symbols: int = 0
    uncovered_symbols: int = 0
    coverage_rate: float = 0.0
    supplementary_count: int = 0
    gaps: list[str] = Field(default_factory=list)


def _evidence_blobs(doc_runs: list[RetrievalRun]) -> list[str]:
    """Lower-cased text blobs for every piece of evidence in the retrieval runs."""
    blobs: list[str] = []
    for run in doc_runs:
        for ev in run.top_doc_evidence:
            parts = [ev.title, " ".join(ev.heading_path), ev.chunk_title, ev.snippet]
            blob = " ".join(p for p in parts if p).lower()
            if blob:
                blobs.append(blob)
    return blobs


def compute_coverage(
    code_report: CodeEvidenceReport, doc_runs: list[RetrievalRun]
) -> CoverageResult:
    """Deterministically check whether doc evidence covers every used code symbol.

    A symbol is *covered* when its identifier is found (case-insensitive,
    identifier-bounded) in at least one retrieved doc-evidence blob. The match
    normalises only the intra-identifier naming style so ``declarative_base`` also
    matches ``DeclarativeBase`` / ``declarative-base`` / ``declarative base``, while
    unrelated prose between the components never counts as a hit. This is
    intentionally simple and reproducible: it neither needs a model nor depends on
    ranking beyond what the retrieval tool already returned.
    """
    by_symbol: dict[str, list[CodeUsage]] = {}
    for usage in code_report.usages:
        by_symbol.setdefault(usage.symbol, []).append(usage)

    blobs = _evidence_blobs(doc_runs)

    gaps: list[CoverageGap] = []
    covered = 0
    for symbol, usages in by_symbol.items():
        if _is_symbol_covered(symbol, blobs):
            covered += 1
            continue
        gaps.append(
            CoverageGap(
                symbol=symbol,
                usage_count=len(usages),
                sample_paths=sorted({u.path for u in usages})[:3],
                reason="no doc evidence mentions this symbol",
            )
        )

    total = len(by_symbol)
    rate = (covered / total) if total else 0.0
    return CoverageResult(
        total_symbols=total,
        covered_symbols=covered,
        uncovered_symbols=len(gaps),
        coverage_rate=rate,
        gaps=gaps,
    )


def gap_query(
    gap: CoverageGap,
    *,
    package: str,
    source_version: str,
    target_version: str,
    user_intent: str,
) -> str:
    """Deterministic supplementary retrieval query for ``fake`` mode."""
    tokens: list[str] = [package, gap.symbol]
    if target_version:
        tokens.append(target_version)
    elif source_version:
        tokens.append(source_version)
    extra = (user_intent or "").strip()
    if extra:
        tokens.append(extra)
    tokens.append("migration upgrade")
    return " ".join(t for t in tokens if t)


def summarize(result: CoverageResult, supplementary_count: int) -> CoverageSummary:
    """Reduce a :class:`CoverageResult` into the plan-stored :class:`CoverageSummary`."""
    return CoverageSummary(
        total_symbols=result.total_symbols,
        covered_symbols=result.covered_symbols,
        uncovered_symbols=result.uncovered_symbols,
        coverage_rate=result.coverage_rate,
        supplementary_count=supplementary_count,
        gaps=[g.symbol for g in result.gaps],
    )
