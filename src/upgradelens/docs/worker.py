"""Background worker that persists S17 backfill jobs into the corpus.

The retrieval tool enqueues :class:`~upgradelens.db.models.DocIngestJob` rows
(see ``docs/jobs.py``). This module drains them: it re-fetches each source through
the *same* SSRF-restricted :class:`~upgradelens.tools.fetcher.RestrictedFetcher`
the live path uses, then persists the cleaned text into the shared corpus via
``persist_source_text`` -- so the next retrieval for that package finds it locally.

The worker lives in a separate process (``upgradelens rag-worker``). Online fetches
honour the ``NetworkMode`` policy exactly like the live path: in ``offline`` mode a
job is *skipped*, never fetched.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, cast

from sqlalchemy.orm import Session

from upgradelens.config import NetworkMode
from upgradelens.db.database import session_for
from upgradelens.db.models import DocIngestJob, DocIngestJobStatus
from upgradelens.docs.ingest import persist_source_text
from upgradelens.docs.jobs import pending_jobs
from upgradelens.domain.doc_source_spec import DocSourceSpec, TrustLevel
from upgradelens.tools.errors import (
    FetchTimeoutError,
    HttpError,
    OutOfNetworkError,
    TooLargeError,
    ToolError,
    TooManyRedirectsError,
)
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.trace import ToolTrace

logger = logging.getLogger(__name__)

_FETCH_EXCEPTIONS = (
    OutOfNetworkError,
    HttpError,
    TooLargeError,
    FetchTimeoutError,
    TooManyRedirectsError,
    ToolError,
)


def _spec_for_job(job: DocIngestJob) -> DocSourceSpec:
    source_id = (
        f"online:{job.package_name}:"
        f"{hashlib.sha1(job.source_url.encode('utf-8')).hexdigest()[:16]}"
    )
    return DocSourceSpec(
        id=source_id,
        package_name=job.package_name,
        url=job.source_url,
        title=job.source_title or job.package_name,
        source_type="official_doc",
        # Trust is never auto-upgraded: an online source ingested as "community"
        # stays "community" until the verification layer promotes it.
        trust_level=cast(TrustLevel, job.trust_level),
        source_version_spec=job.source_version_spec,
        target_version_spec=job.target_version_spec,
        fetch_strategy="html",
    )


def run_ingest_job(
    job: DocIngestJob,
    *,
    session: Session,
    fetcher: RestrictedFetcher,
    network: str = "online_fallback",
    embedding: Any = None,
    trace: Any = None,
) -> str:
    """Process one job: re-fetch via the guarded fetcher, persist into the corpus.

    Updates ``job.status`` and returns the resulting status value. Failures are
    recorded on the job row (never raised) so the worker can keep draining the
    queue.
    """
    trace = trace or ToolTrace()
    job.status = DocIngestJobStatus.PROCESSING.value
    job.attempts += 1
    session.commit()

    if NetworkMode(network) == NetworkMode.OFFLINE:
        job.status = DocIngestJobStatus.SKIPPED.value
        job.reason = "offline"
        session.commit()
        trace.record(
            tool="ingest_skip",
            target=job.source_url,
            status="ok",
            params={"reason": "offline"},
        )
        return job.status

    try:
        content = fetcher.fetch(job.source_url)
    except _FETCH_EXCEPTIONS as exc:
        job.status = DocIngestJobStatus.FAILED.value
        job.error = f"fetch failed: {exc}"
        session.commit()
        trace.record(tool="ingest_fetch", target=job.source_url, status="error", error=str(exc))
        return job.status

    raw = content.content.decode("utf-8", errors="replace")
    try:
        record = persist_source_text(
            session,
            _spec_for_job(job),
            raw,
            "",  # online: no offline snapshot file
            trust_level=cast(TrustLevel, job.trust_level),
            embedding=embedding,
        )
    except Exception as exc:  # noqa: BLE001 - record, never crash the worker
        job.status = DocIngestJobStatus.FAILED.value
        job.error = f"persist failed: {exc}"
        session.commit()
        trace.record(tool="ingest_persist", target=job.source_url, status="error", error=str(exc))
        return job.status

    job.status = DocIngestJobStatus.DONE.value
    job.chunks_collected = record.chunk_count
    session.commit()
    trace.record(
        tool="ingest_done",
        target=job.source_url,
        status="ok",
        params={"source_id": record.id, "chunks": record.chunk_count},
    )
    return job.status


def process_pending_jobs(
    engine: Any,
    *,
    limit: int = 10,
    network: str = "online_fallback",
    embedding: Any = None,
) -> dict[str, int]:
    """Drain up to ``limit`` pending jobs and persist them into the corpus.

    Returns a tally of job outcomes by status. A worker crash on one job is
    isolated: the job is marked failed and the next job is processed.
    """
    session = session_for(engine)()
    counts: dict[str, int] = {s.value: 0 for s in DocIngestJobStatus}
    try:
        jobs = pending_jobs(session, limit=limit)
        fetcher = RestrictedFetcher(trace=ToolTrace())
        for job in jobs:
            try:
                status = run_ingest_job(
                    job,
                    session=session,
                    fetcher=fetcher,
                    network=network,
                    embedding=embedding,
                )
            except Exception as exc:  # noqa: BLE001 - isolate a per-job crash
                status = DocIngestJobStatus.FAILED.value
                job.status = status
                job.error = f"worker crash: {exc}"
                session.commit()
                logger.warning("ingest job %s crashed: %s", job.id, exc)
            counts[status] = counts.get(status, 0) + 1
    finally:
        session.close()
    return counts
