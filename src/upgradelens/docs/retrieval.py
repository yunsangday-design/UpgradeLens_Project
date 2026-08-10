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

from packaging.utils import canonicalize_name
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.db.vector import (
    EmbeddingBackend,
    SqliteVecIndex,
    VectorIndex,
    VectorIndexUnavailable,
)
from upgradelens.docs.ingest import iter_sources_for_package
from upgradelens.domain.doc_evidence import DocEvidence, RetrievalRun, _stable_sha256
from upgradelens.domain.skill import SkillPackage
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.llm.query_rewrite import rewrite_query

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


def _stable_evidence_id(source_id: str, snapshot_hash: str, content_hash: str) -> str:
    return "doc:" + _stable_sha256(f"{source_id}|{snapshot_hash}|{content_hash}")[:16]


def _to_evidence(
    session: Session, source_id: str, chunk: models.DocChunkRow, query: str, score: float
) -> DocEvidence:
    source_row = session.get(models.DocSourceRow, source_id)
    url = source_row.url if source_row is not None else ""
    title = source_row.title if source_row is not None else ""
    snapshot_hash = source_row.snapshot_hash if source_row is not None else ""
    package_name = source_row.package_name if source_row is not None else ""
    source_version_spec = source_row.source_version_spec if source_row is not None else ""
    target_version_spec = source_row.target_version_spec if source_row is not None else ""
    trust_level = source_row.trust_level if source_row is not None else ""
    snippet = chunk.content[:240]
    return DocEvidence(
        evidence_id=_stable_evidence_id(source_id, snapshot_hash, chunk.content_hash),
        source_id=source_id,
        url=url,
        title=title,
        chunk_title=chunk.title,
        heading_path=chunk.heading_path_list,
        snapshot_hash=snapshot_hash,
        snippet=snippet,
        score=round(score, 4),
        matched_query=query,
        package_name=package_name,
        source_version_spec=source_version_spec,
        target_version_spec=target_version_spec,
        trust_level=trust_level,
        chunk_content_hash=chunk.content_hash,
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


# --------------------------------------------------------------------------- #
# Shared-corpus retrieval (stage B2): no dedicated Skill required
# --------------------------------------------------------------------------- #


def _version_terms(spec: str) -> list[str]:
    """Turn a version spec into a small set of FTS-friendly version tokens.

    ``"1.x"`` becomes ``["v1"]`` and ``"2.0"`` becomes ``["v2"]`` -- enough to
    bias the search toward version-specific sections without flooding it with
    the bare digits that would match almost every chunk.
    """
    if not spec:
        return []
    match = re.search(r"(\d+)", spec)
    return [f"v{match.group(1)}"] if match else []


def _package_query_terms(
    user_intent: str,
    source_version: str,
    target_version: str,
    code_symbols: list[str],
) -> list[str]:
    """Fuse the signals that locate the right documentation section.

    Order is preserved and duplicates dropped; :func:`build_fts_query` performs
    the final stop-word and length filtering.
    """
    raw: list[str] = []
    if user_intent:
        raw.extend(user_intent.split())
    raw.extend(_version_terms(source_version))
    raw.extend(_version_terms(target_version))
    raw.extend(code_symbols)
    seen: set[str] = set()
    terms: list[str] = []
    for token in raw:
        token = token.strip()
        if token and token not in seen:
            seen.add(token)
            terms.append(token)
    return terms


def _retrieve_across(
    session: Session,
    source_ids: list[str],
    query: str,
    *,
    top_k: int,
    record: bool,
) -> list[RetrievalRun]:
    """Run one FTS5 query over every source of a package, one run per source."""
    return [
        retrieve(session, source_id, query, top_k=top_k, record=record) for source_id in source_ids
    ]


def retrieve_for_package(
    session: Session,
    package: str,
    source_version: str,
    target_version: str,
    user_intent: str,
    code_symbols: list[str],
    *,
    source_id: str | None = None,
    gateway: ModelGateway | None = None,
    mode: ModelMode | str = ModelMode.FAKE,
    embedding: EmbeddingBackend | None = None,
    top_k: int = 5,
    curated_queries: list[str] | None = None,
    record: bool = False,
) -> list[RetrievalRun]:
    """Main retrieval path backed by the shared corpus, not a dedicated Skill.

    Queries are produced by :func:`rewrite_query` -- a deterministic fused query
    in ``fake`` mode, or several LLM-expanded natural-language queries in
    ``live`` mode (curated boosts from an optional pack are always appended).
    Every query is run through FTS5; when an embedding backend is configured and
    ``sqlite-vec`` is loadable, the same queries are also run through the vector
    index and the two ranked lists are fused per source with Reciprocal Rank
    Fusion (RRF). When the vector index is unavailable the function degrades to
    the FTS5-only behaviour, so a missing extension never costs doc evidence.

    All ranked lists are returned as :class:`RetrievalRun` objects; the caller
    (``build_bundle``) de-duplicates overlapping chunks by ``evidence_id``. With
    no curated queries the path still surfaces documentation purely from the
    shared corpus and the discovered symbols, so the pipeline no longer needs a
    dedicated Skill to collect doc evidence.
    """
    specs = iter_sources_for_package(session, canonicalize_name(package))
    if source_id:
        specs = [s for s in specs if s.id == source_id]
    if not specs:
        return []
    source_ids = [s.id for s in specs]

    queries = _build_retrieval_queries(
        gateway=gateway,
        package=package,
        source_version=source_version,
        target_version=target_version,
        user_intent=user_intent,
        code_symbols=code_symbols,
        mode=mode,
        curated_queries=curated_queries,
    )
    if not queries:
        return []

    # Optional hybrid vector recall (stage B4). When sqlite-vec cannot be loaded
    # or no embedding backend is configured, ``_prepare_vector_recall`` returns
    # ``None`` and we degrade to the FTS5-only behaviour the caller expects.
    vec_index, vec_search, chunk_source = _prepare_vector_recall(
        session, embedding, queries, top_k, len(source_ids)
    )

    boost_terms = frozenset(code_symbols)
    runs: list[RetrievalRun] = []
    for query in queries:
        for sid in source_ids:
            fts_run = retrieve(
                session, sid, query, top_k=top_k, boost_terms=boost_terms, record=record
            )
            if vec_index is None:
                runs.append(fts_run)
                continue
            runs.append(
                _fuse_source(
                    session,
                    source_id=sid,
                    query=query,
                    fts_run=fts_run,
                    vec_hits=vec_search.get(query, []),
                    chunk_source=chunk_source,
                    top_k=top_k,
                )
            )
    return runs


def _build_retrieval_queries(
    *,
    gateway: ModelGateway | None,
    package: str,
    source_version: str,
    target_version: str,
    user_intent: str,
    code_symbols: list[str],
    mode: ModelMode | str,
    curated_queries: list[str] | None,
) -> list[str]:
    """Assemble every query to run: curated boosts plus the rewritten/fused query.

    In ``live`` mode :func:`rewrite_query` expands the intent into several
    natural-language queries; in ``fake`` mode it returns the single deterministic
    fused query. Curated queries (from an optional pack) are always kept as extra
    boosts and de-duplicated against the base queries.
    """
    base = rewrite_query(
        gateway,
        package=package,
        source_version=source_version,
        target_version=target_version,
        user_intent=user_intent,
        code_symbols=code_symbols,
        mode=mode,
    )
    queries: list[str] = []
    seen: set[str] = set()
    for q in (curated_queries or []) + base:
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


def _prepare_vector_recall(
    session: Session,
    embedding: EmbeddingBackend | None,
    queries: list[str],
    top_k: int,
    n_sources: int,
) -> tuple[VectorIndex | None, dict[str, list[tuple[int, float]]], dict[int, str]]:
    """Open the vector index and run every query through it once.

    Returns ``(index, per_query_hits, chunk_source_map)``. When the index is not
    usable the first element is ``None`` and the other two are empty, signalling
    the caller to skip fusion and use FTS5-only.
    """
    if embedding is None or not embedding.available():
        return None, {}, {}
    try:
        vec_index: VectorIndex = SqliteVecIndex(session, embedding.dimension)
    except VectorIndexUnavailable:
        return None, {}, {}

    per_query: dict[str, list[tuple[int, float]]] = {}
    chunk_ids: set[int] = set()
    for q in queries:
        vectors = embedding.embed([q])
        if not vectors:
            per_query[q] = []
            continue
        matches = vec_index.search(vectors[0], top_k=max(top_k, top_k * n_sources))
        pairs = [(m.chunk_id, m.distance) for m in matches]
        per_query[q] = pairs
        chunk_ids.update(cid for cid, _ in pairs)

    chunk_source: dict[int, str] = {}
    if chunk_ids:
        rows = session.execute(
            select(models.DocChunkRow.id, models.DocChunkRow.source_id).where(
                models.DocChunkRow.id.in_(chunk_ids)
            )
        ).all()
        chunk_source = {r[0]: r[1] for r in rows}
    return vec_index, per_query, chunk_source


def _fuse_source(
    session: Session,
    *,
    source_id: str,
    query: str,
    fts_run: RetrievalRun,
    vec_hits: list[tuple[int, float]],
    chunk_source: dict[int, str],
    top_k: int,
) -> RetrievalRun:
    """Reciprocal Rank Fusion of the FTS5 and vector ranked lists for one source.

    Each candidate chunk gets ``1/(k+rank)`` from whichever lists it appears in,
    summed. The API-symbol boost that the FTS5 reranker already applies keeps the
    right chunks near the top of the FTS5 side, so it flows into the fused score.
    """
    fts_rank = {cid: i for i, cid in enumerate(fts_run.matched_chunk_ids)}
    vec_hits_src = [(cid, dist) for (cid, dist) in vec_hits if chunk_source.get(cid) == source_id]
    vec_rank = {cid: i for i, (cid, _d) in enumerate(vec_hits_src)}

    candidate_ids = set(fts_rank) | set(vec_rank)
    if not candidate_ids:
        return RetrievalRun(
            run_id=uuid4().hex,
            source_id=source_id,
            query=query,
            matched_chunk_ids=[],
            top_doc_evidence=[],
            generated_at=_utc_now(),
        )

    rows = {
        r.id: r
        for r in session.execute(
            select(models.DocChunkRow).where(models.DocChunkRow.id.in_(candidate_ids))
        ).scalars()
    }
    k = 60.0
    scored: list[tuple[float, int]] = []
    for cid in candidate_ids:
        fts_rrf = 1.0 / (k + fts_rank[cid] + 1) if cid in fts_rank else 0.0
        vec_rrf = 1.0 / (k + vec_rank[cid] + 1) if cid in vec_rank else 0.0
        scored.append((fts_rrf + vec_rrf, cid))
    scored.sort(key=lambda item: -item[0])
    top = scored[:top_k]
    evidence = [_to_evidence(session, source_id, rows[cid], query, score) for score, cid in top]
    return RetrievalRun(
        run_id=uuid4().hex,
        source_id=source_id,
        query=query,
        matched_chunk_ids=[cid for _, cid in top],
        top_doc_evidence=evidence,
        generated_at=_utc_now(),
    )
