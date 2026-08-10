"""Live query rewriting for retrieval (stage B4).

In ``fake`` mode retrieval uses a deterministic fused query (the same signal
fusion as stage B2). In ``live`` mode a small LLM call expands the structured
intent into several natural-language queries -- synonyms, API names and
version-specific phrasings -- which are then run through both FTS5 and (when
available) the vector index. The rewrite is best-effort: any failure falls back
to the deterministic query, so the pipeline never loses doc evidence because the
rewriter is unavailable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.llm.prompts import QUERY_REWRITER


class QueryRewrites(BaseModel):
    """Structured output of the query rewriter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    queries: list[str] = []


def _deterministic_query(
    user_intent: str,
    source_version: str,
    target_version: str,
    code_symbols: list[str],
) -> str:
    """The same fused FTS5 query used by the fake retrieval path (stage B2)."""
    from upgradelens.docs.retrieval import _package_query_terms, build_fts_query

    terms = _package_query_terms(user_intent, source_version, target_version, code_symbols)
    return build_fts_query(" ".join(terms))


def rewrite_query(
    gateway: ModelGateway | None,
    *,
    package: str,
    source_version: str,
    target_version: str,
    user_intent: str,
    code_symbols: list[str],
    mode: ModelMode | str = ModelMode.FAKE,
) -> list[str]:
    """Return the retrieval queries for this request.

    * ``fake`` / no gateway -> one deterministic fused FTS5 query.
    * ``live`` + gateway  -> several LLM-expanded natural-language queries.

    On any failure the deterministic query is returned. The result is never
    empty when the deterministic query exists, because an empty query list would
    mean "no doc evidence at all".
    """
    deterministic = _deterministic_query(user_intent, source_version, target_version, code_symbols)
    fallback = [deterministic] if deterministic else []

    if gateway is None or ModelMode(mode) != ModelMode.LIVE:
        return fallback

    prompt = QUERY_REWRITER.render(
        package=package,
        source_version=source_version,
        target_version=target_version,
        user_intent=user_intent or "(none)",
        code_symbols="\n".join(f"- {s}" for s in code_symbols) or "(none)",
    )
    try:
        result, _ = gateway.complete_structured(
            prompt=prompt, schema=QueryRewrites, name="query_rewriter"
        )
    except Exception:
        return fallback
    queries = [q for q in (result.queries or []) if q.strip()]
    return queries or fallback
