"""Retrieval evaluation baseline (ROADMAP Step 4, B0).

Measure the *current* curated FTS5 retrieval path
(``skill.sources`` × ``pattern.retrieval_queries`` with a ``pattern.match`` boost)
so that any later retrieval change (sqlite-vec hybrid, query rewriting, ...) can be
compared against a reproducible number. No vector index is touched here — this is a
FTS5-only baseline, intentionally rollback-safe.

A case is ``(package, source_version, target_version, pattern_id, code_symbols) →
expected_chunks`` and the metrics are recall@k, MRR and top-k hit rate over the
ranked evidence list returned by the curated path.

Note: this baseline is intentionally *per-pattern* and *FTS5-only*.
``build_pattern_ranking`` applies only the active pattern's ``match`` boost, whereas the
live curated path (``retrieve_skill_evidence``) uses the union of *all* patterns' ``match``
terms; the baseline is therefore slightly stricter than the real curated path, which is a
safe direction for a regression guard (a future path must beat a harder baseline). The case's
``code_symbols`` are recorded but not consumed yet — they become inputs to the query builder
in later steps (B2/B4), since the FTS5 baseline relies solely on ``pattern.retrieval_queries``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.db.vector import EmbeddingBackend, SqliteVecIndex, VectorIndexUnavailable
from upgradelens.docs import embeddings as _embeddings
from upgradelens.docs.ingest import ingest_skill
from upgradelens.docs.retrieval import retrieve, retrieve_for_package
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.domain.skill import SkillPackage, UsagePattern
from upgradelens.skills import builtin_registry

#: Cut-off ranks reported by the baseline.
_KS = (1, 3, 5, 10)


class RetrievalCase(BaseModel):
    """One labelled retrieval query: which chunk *should* the curated path surface."""

    case_id: str
    package: str
    source_version: str
    target_version: str
    pattern_id: str
    code_symbols: list[str] = Field(default_factory=list)
    #: Leaf chunk titles (``heading_path[-1]``) that must be recalled.
    expected_chunks: list[str]


class RetrievalCaseResult(BaseModel):
    case_id: str
    package: str
    pattern_id: str
    expected_chunks: list[str]
    top_chunks: list[str]
    ranks: list[int | None]
    best_rank: int | None = None
    hit: bool = False
    mrr: float = 0.0
    recall_at_k: dict[str, float] = Field(default_factory=dict)
    top_k_hit: dict[str, bool] = Field(default_factory=dict)


class RetrievalBaselineReport(BaseModel):
    schema_version: int = 1
    generated_at: str
    top_k: int
    n_cases: int
    cases: list[RetrievalCaseResult]
    summary: dict[str, Any] = Field(default_factory=dict)
    notes: str = (
        "FTS5-only curated retrieval baseline (skill.sources x pattern.retrieval_queries "
        "with pattern.match boost). No vector index used; rollback-safe."
    )


def load_retrieval_cases(directory: Path) -> list[RetrievalCase]:
    cases: list[RetrievalCase] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cases.append(RetrievalCase(**data))
    return cases


def build_pattern_ranking(
    session: Session, skill: SkillPackage, pattern: UsagePattern, *, top_k: int = 5
) -> list[list[str]]:
    """Replicate the curated retrieval path but keep a global ranked chunk list.

    Mirrors ``retrieve_skill_evidence`` (skill sources x pattern queries) but preserves
    the ordering so we can compute recall@k / MRR. A chunk's score is ``-bm25_rank +
    boost``, exactly as :func:`retrieve` computes it.
    """
    boost_terms = frozenset({pattern.match})
    collected: dict[tuple[str, ...], float] = {}
    for source in skill.sources:
        if not source.fixture_snapshot:
            continue
        for query in pattern.retrieval_queries:
            run = retrieve(
                session,
                source.id,
                query,
                top_k=top_k,
                boost_terms=boost_terms,
                record=False,
            )
            for ev in run.top_doc_evidence:
                key = tuple(ev.heading_path)
                score = ev.score
                if key not in collected or score > collected[key]:
                    collected[key] = score
    return [list(hp) for hp, _ in sorted(collected.items(), key=lambda kv: -kv[1])]


def _evaluate_ranking(
    ranking: list[list[str]], expected_leaves: list[str], *, top_k: int
) -> tuple[list[str], list[int | None], int | None, bool, float, dict[str, float], dict[str, bool]]:
    leaf_ranking = [(hp[-1] if hp else "") for hp in ranking]
    ranks: list[int | None] = []
    for exp in expected_leaves:
        try:
            ranks.append(leaf_ranking.index(exp) + 1)
        except ValueError:
            ranks.append(None)
    found = [r for r in ranks if r is not None]
    best = min(found) if found else None
    hit = best is not None
    mrr = 1.0 / best if best is not None else 0.0
    recall_at_k = {
        str(k): (
            len([r for r in found if r <= k]) / len(expected_leaves) if expected_leaves else 0.0
        )
        for k in _KS
    }
    top_k_hit = {str(k): (best is not None and best <= k) for k in _KS}
    return leaf_ranking[:top_k], ranks, best, hit, mrr, recall_at_k, top_k_hit


def ingest_skills_for_baseline(session: Session, skills: list[SkillPackage]) -> int:
    """Ingest every built-in skill that ships an offline fixture; return source count."""
    total = 0
    for skill in skills:
        total += len(ingest_skill(session, skill))
    return total


def run_retrieval_baseline(
    session: Session,
    skills: list[SkillPackage],
    cases: list[RetrievalCase],
    *,
    top_k: int = 5,
) -> RetrievalBaselineReport:
    skill_by_pkg: dict[str, SkillPackage] = {}
    for s in skills:
        for name in s.package_names:
            skill_by_pkg[name] = s
    results: list[RetrievalCaseResult] = []
    for case in cases:
        skill = skill_by_pkg.get(case.package)
        pattern = None
        if skill is not None:
            pattern = next((p for p in skill.patterns if p.id == case.pattern_id), None)
        if skill is None or pattern is None:
            sys.stderr.write(f"upgradelens: skipping unknown case {case.case_id}\n")
            continue
        ranking = build_pattern_ranking(session, skill, pattern, top_k=top_k)
        top_chunks, ranks, best, hit, mrr, recall_at_k, top_k_hit = _evaluate_ranking(
            ranking, case.expected_chunks, top_k=top_k
        )
        results.append(
            RetrievalCaseResult(
                case_id=case.case_id,
                package=case.package,
                pattern_id=case.pattern_id,
                expected_chunks=case.expected_chunks,
                top_chunks=top_chunks,
                ranks=ranks,
                best_rank=best,
                hit=hit,
                mrr=mrr,
                recall_at_k=recall_at_k,
                top_k_hit=top_k_hit,
            )
        )

    n = len(results)
    summary: dict[str, Any] = {
        "n_cases": n,
        "avg_mrr": (sum(r.mrr for r in results) / n) if n else 0.0,
        "avg_recall_at_k": {
            str(k): (sum(r.recall_at_k[str(k)] for r in results) / n) if n else 0.0 for k in _KS
        },
        "avg_top_k_hit_rate": {
            str(k): (sum(1 for r in results if r.top_k_hit[str(k)]) / n) if n else 0.0 for k in _KS
        },
    }
    return RetrievalBaselineReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        top_k=top_k,
        n_cases=n,
        cases=results,
        summary=summary,
    )


def run_baseline(
    session: Session,
    skills: list[SkillPackage],
    cases: list[RetrievalCase],
    *,
    top_k: int = 5,
) -> RetrievalBaselineReport:
    """Convenience: ingest the skills' fixtures, then evaluate every case."""
    ingest_skills_for_baseline(session, skills)
    return run_retrieval_baseline(session, skills, cases, top_k=top_k)


def _flatten_runs_leaves(runs: list[RetrievalRun]) -> list[str]:
    """De-duplicate a per-(query x source) run list into one ranked leaf list."""
    seen: set[str] = set()
    leaves: list[str] = []
    for run in runs:
        for ev in run.top_doc_evidence:
            if ev.evidence_id in seen:
                continue
            seen.add(ev.evidence_id)
            leaves.append(ev.heading_path[-1] if ev.heading_path else "")
    return leaves


def run_hybrid_baseline(
    session: Session,
    skills: list[SkillPackage],
    cases: list[RetrievalCase],
    *,
    top_k: int = 5,
    embedding: EmbeddingBackend | None = None,
) -> RetrievalBaselineReport:
    """Hybrid (FTS5 + sqlite-vec) retrieval baseline, opt-in via ``embedding``.

    Mirrors :func:`run_retrieval_baseline` but routes each case through
    :func:`retrieve_for_package` with the vector index enabled, so the FTS5-only
    numbers and the hybrid numbers can be compared directly. When ``embedding``
    is ``None`` (no configured model) the function degrades to the FTS5-only
    retrieve, so callers never need a vector backend to get a baseline.
    """
    if embedding is not None and embedding.available():
        try:
            SqliteVecIndex(session, embedding.dimension).rebuild(session, embedding)
        except VectorIndexUnavailable:
            pass  # fall back to FTS5-only for this case set

    skill_by_pkg: dict[str, SkillPackage] = {}
    for s in skills:
        for name in s.package_names:
            skill_by_pkg[name] = s
    results: list[RetrievalCaseResult] = []
    for case in cases:
        skill = skill_by_pkg.get(case.package)
        pattern = None
        if skill is not None:
            pattern = next((p for p in skill.patterns if p.id == case.pattern_id), None)
        curated = list(pattern.retrieval_queries) if pattern is not None else []
        runs = retrieve_for_package(
            session,
            case.package,
            case.source_version,
            case.target_version,
            "",
            case.code_symbols,
            curated_queries=curated,
            top_k=top_k,
            embedding=embedding,
        )
        leaves = _flatten_runs_leaves(runs)
        top_chunks, ranks, best, hit, mrr, recall_at_k, top_k_hit = _evaluate_ranking(
            [[leaf] for leaf in leaves], case.expected_chunks, top_k=top_k
        )
        results.append(
            RetrievalCaseResult(
                case_id=case.case_id,
                package=case.package,
                pattern_id=case.pattern_id,
                expected_chunks=case.expected_chunks,
                top_chunks=top_chunks,
                ranks=ranks,
                best_rank=best,
                hit=hit,
                mrr=mrr,
                recall_at_k=recall_at_k,
                top_k_hit=top_k_hit,
            )
        )

    n = len(results)
    summary: dict[str, Any] = {
        "n_cases": n,
        "avg_mrr": (sum(r.mrr for r in results) / n) if n else 0.0,
        "avg_recall_at_k": {
            str(k): (sum(r.recall_at_k[str(k)] for r in results) / n) if n else 0.0 for k in _KS
        },
        "avg_top_k_hit_rate": {
            str(k): (sum(1 for r in results if r.top_k_hit[str(k)]) / n) if n else 0.0 for k in _KS
        },
    }
    return RetrievalBaselineReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        top_k=top_k,
        n_cases=n,
        cases=results,
        summary=summary,
        notes=(
            "Hybrid FTS5 + sqlite-vec retrieval baseline (RRF fusion). "
            "Vector index used only when an embedding backend is configured; "
            "otherwise identical to the FTS5-only baseline."
        ),
    )


def render_retrieval_baseline_markdown(report: RetrievalBaselineReport) -> str:
    lines = [
        "# Retrieval Baseline (FTS5-only curated path)",
        "",
        f"- cases: **{report.n_cases}**, top_k: **{report.top_k}**",
        f"- generated: {report.generated_at}",
        "",
        "## Per-case",
        "",
        "| case | pattern | expected (best recalled) | best rank | hit@5 | MRR |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in report.cases:
        expected_top = r.expected_chunks[0] if r.expected_chunks else ""
        top1 = r.top_chunks[0] if r.top_chunks else ""
        lines.append(
            f"| {r.case_id} | {r.pattern_id} | {expected_top} -> `{top1}` | "
            f"{r.best_rank or '—'} | {'yes' if r.top_k_hit['5'] else 'no'} | {r.mrr:.3f} |"
        )
    s = report.summary
    lines += [
        "",
        "## Summary",
        "",
        f"- mean reciprocal rank (MRR): **{s['avg_mrr']:.3f}**",
    ]
    for k in _KS:
        lines.append(
            f"- recall@{k}: **{s['avg_recall_at_k'][str(k)]:.3f}** "
            f"(top-{k} hit-rate {s['avg_top_k_hit_rate'][str(k)]:.0%})"
        )
    lines += ["", f"> {report.notes}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - manual eval harness
    """CLI for the B0 retrieval baseline.

    Default runs the FTS5-only curated path. Pass ``--hybrid`` together with an
    ``--embedding-url``/``--embedding-model`` pair to compare the hybrid
    FTS5+sqlite-vec path; when the embedding backend is unreachable the harness
    prints a warning and falls back to FTS5-only so the run never fails.
    """
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="UpgradeLens B0 retrieval baseline")
    parser.add_argument("--db", required=True)
    parser.add_argument("--cases-dir", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--hybrid", action="store_true")
    parser.add_argument("--embedding-url", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-key", default="")
    args = parser.parse_args(argv)

    engine = engine_for(args.db)
    init_db(engine)
    session = session_for(engine)()
    try:
        skills = builtin_registry().all()
        ingest_skills_for_baseline(session, skills)
        cases = load_retrieval_cases(Path(args.cases_dir))

        embedding: EmbeddingBackend | None = None
        if args.hybrid:
            embedding = _embeddings.embedding_from_config(
                enabled=True,
                base_url=args.embedding_url,
                model=args.embedding_model,
                api_key=args.embedding_key,
            )
            if not embedding.available():
                sys.stderr.write("upgradelens: embedding backend unavailable; using FTS5-only\n")
                embedding = None

        if embedding is not None and embedding.available():
            report = run_hybrid_baseline(
                session, skills, cases, top_k=args.top_k, embedding=embedding
            )
        else:
            report = run_retrieval_baseline(session, skills, cases, top_k=args.top_k)
        print(render_retrieval_baseline_markdown(report))
    finally:
        session.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual eval harness
    raise SystemExit(main())
