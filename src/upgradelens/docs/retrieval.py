"""Keyword RAG over ingested documentation (stage 4).

Pipeline: a natural-language ``query`` (or a Skill ``retrieval_queries`` phrase)
is turned into an FTS5 query, the ``doc_chunks_fts`` index returns ranked
candidates, and a rule-based reranker boosts chunks whose text contains known
API names (the ``match`` terms of the Skill's usage patterns). The result is a
:class:`~upgradelens.domain.doc_evidence.RetrievalRun` of citable
:class:`~upgradelens.domain.doc_evidence.DocEvidence`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.domain.doc_evidence import DocEvidence, RetrievalRun
from upgradelens.domain.skill import SkillPackage

_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "or",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "how",
    "do",
    "i",
    "we",
    "you",
    "use",
    "using",
    "from",
    "this",
    "that",
    "when",
    "what",
    "which",
    "be",
    "by",
    "as",
    "at",
    "it",
    "its",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_fts_query(query: str) -> str:
    """Turn free text into an FTS5 ``MATCH`` expression (OR of quoted terms)."""
    words = re.findall(r"[A-Za-z0-9_]+", query.lower())
    terms = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    if not terms:
        return "__no_terms__"  # token that cannot exist → empty result, no error
    quoted = [f'"{t.replace(chr(34), chr(39))}"' for t in terms]
    return " OR ".join(quoted)


def _to_evidence(
    session: Session, source_id: str, chunk: models.DocChunkRow, query: str, score: float
) -> DocEvidence:
    source_row = session.get(models.DocSourceRow, source_id)
    url = source_row.url if source_row is not None else ""
    title = source_row.title if source_row is not None else ""
    snapshot_hash = source_row.snapshot_hash if source_row is not None else ""
    snippet = chunk.content[:240]
    return DocEvidence(
        source_id=source_id,
        url=url,
        title=title,
        chunk_title=chunk.title,
        heading_path=chunk.heading_path_list,
        snapshot_hash=snapshot_hash,
        snippet=snippet,
        score=round(score, 4),
        matched_query=query,
    )


def retrieve(
    session: Session,
    source_id: str,
    query: str,
    *,
    top_k: int = 5,
    boost_terms: frozenset[str] = frozenset(),
    record: bool = True,
) -> RetrievalRun:
    """Run one keyword retrieval over ``source_id`` and return ranked evidence."""
    fts_query = build_fts_query(query)
    rows = session.execute(
        text(
            "SELECT rowid, rank FROM doc_chunks_fts "
            "WHERE doc_chunks_fts MATCH :q AND source_id = :sid ORDER BY rank"
        ),
        {"q": fts_query, "sid": source_id},
    ).all()
    ranks = {row[0]: float(row[1]) for row in rows}
    if not ranks:
        run = RetrievalRun(
            run_id=uuid4().hex,
            source_id=source_id,
            query=query,
            matched_chunk_ids=[],
            top_doc_evidence=[],
            generated_at=_utc_now(),
        )
        if record:
            _record_run(session, run)
        return run

    chunk_rows = (
        session.execute(
            select(models.DocChunkRow).where(models.DocChunkRow.id.in_(list(ranks.keys())))
        )
        .scalars()
        .all()
    )
    scored: list[tuple[float, models.DocChunkRow]] = []
    for chunk in chunk_rows:
        base = -ranks.get(chunk.id, 0.0)  # FTS5 bm25 rank: more negative = better
        blob = (chunk.title + " " + " ".join(chunk.heading_path_list) + " " + chunk.content).lower()
        boost = sum(1.0 for term in boost_terms if term.lower() in blob)
        scored.append((base + boost, chunk))
    scored.sort(key=lambda item: -item[0])

    top = scored[:top_k]
    evidence = [_to_evidence(session, source_id, chunk, query, score) for score, chunk in top]
    run = RetrievalRun(
        run_id=uuid4().hex,
        source_id=source_id,
        query=query,
        matched_chunk_ids=[chunk.id for _, chunk in top],
        top_doc_evidence=evidence,
        generated_at=_utc_now(),
    )
    if record:
        _record_run(session, run)
    else:
        session.flush()
    return run


def _record_run(session: Session, run: RetrievalRun) -> None:
    row = models.RetrievalRunRow(run_id=run.run_id, source_id=run.source_id, query=run.query)
    row.matched_chunk_ids_list = run.matched_chunk_ids
    session.add(row)
    session.commit()


def retrieve_skill_evidence(
    session: Session, skill: SkillPackage, *, top_k: int = 3
) -> list[DocEvidence]:
    """Aggregate the best evidence across all of a skill's sources and queries."""
    boost_terms = {term for pattern in skill.patterns for term in pattern.match}
    collected: dict[tuple[str, str, tuple[str, ...]], DocEvidence] = {}
    for source in skill.sources:
        if not source.fixture_snapshot:
            continue
        for pattern in skill.patterns:
            for query in pattern.retrieval_queries:
                run = retrieve(
                    session,
                    source.id,
                    query,
                    top_k=top_k,
                    boost_terms=frozenset(boost_terms),
                    record=False,
                )
                for evidence in run.top_doc_evidence:
                    key = (evidence.source_id, evidence.chunk_title, tuple(evidence.heading_path))
                    prev = collected.get(key)
                    if prev is None or evidence.score > prev.score:
                        collected[key] = evidence
    return list(collected.values())
