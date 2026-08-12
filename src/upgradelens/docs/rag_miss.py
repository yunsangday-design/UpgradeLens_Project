"""Structured classification of a local-corpus retrieval miss (S16).

Before the agent may reach for online evidence it must explain *why* the shared
corpus could not serve the request. :class:`RagMissReason` turns the silent
``return []`` in :func:`upgradelens.docs.retrieval.retrieve_for_package` into a
first-class, auditable signal that the trace records as a ``rag_miss`` event.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RagMissReason(StrEnum):
    OK = "ok"  # local corpus served the request; no fallback needed
    NO_DB = "no_db"  # no shared doc store was provided at all
    NO_PACKAGE = "no_package"  # package was never ingested into the corpus
    EMPTY_SOURCE = "empty_source"  # ingested, but no chunks for this version range
    VERSION_MISMATCH = "version_mismatch"  # source exists but not for target version
    QUERY_NO_HIT = "query_no_hit"  # corpus present, retrieval found nothing relevant
    COVERAGE_GAP = "coverage_gap"  # found something, but not enough to verify


def classify_rag_miss(
    *,
    has_db: bool,
    has_sources: bool,
    has_covering_source: bool,
    runs: list[Any],
) -> RagMissReason:
    """Classify why the local corpus could not fully serve the request.

    ``runs`` is the list of :class:`RetrievalRun` already produced by the local
    retrieval step; a run "served" the request when at least one of its
    ``top_doc_evidence`` entries is present.
    """
    if not has_db:
        return RagMissReason.NO_DB
    if not has_sources:
        return RagMissReason.NO_PACKAGE
    if not has_covering_source:
        return RagMissReason.VERSION_MISMATCH
    served = any(getattr(run, "top_doc_evidence", None) for run in runs)
    if not runs or not served:
        return RagMissReason.QUERY_NO_HIT
    # Local retrieval produced evidence but the downstream pipeline may still
    # flag insufficient coverage; callers promote QUERY_NO_HIT -> COVERAGE_GAP
    # when a coverage check fails.
    return RagMissReason.OK
