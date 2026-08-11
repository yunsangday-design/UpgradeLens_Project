"""Enqueue and inspect S17 corpus backfill jobs.

When the local corpus misses and the online fallback (``docs/online_fallback.py``)
actually fetches a documentation source, the retrieval tool records a
:class:`~upgradelens.db.models.DocIngestJob` here. A separate worker
(``docs/worker.py``) later drains those jobs and re-ingests the source into the
shared corpus so the *next* retrieval for the same package hits locally.
"""
from __future__ import annotations

import json
import logging
from typing import cast

from packaging.utils import canonicalize_name
from sqlalchemy import select
from sqlalchemy.orm import Session

from upgradelens.db.models import DocIngestJob, DocIngestJobStatus

logger = logging.getLogger(__name__)


def enqueue_ingest_job(
    session: Session,
    *,
    package: str,
    url: str,
    title: str,
    source_version_spec: str,
    target_version_spec: str,
    trust_level: str,
    discovered: list[tuple[str, str]],
    reason: str = "",
    snapshot_hash: str = "",
) -> DocIngestJob:
    """Record a background job to re-ingest ``url`` into the shared corpus.

    De-duplicates against an already-pending job for the same ``(package, url)`` so
    a repeated miss never piles up duplicate work. The row is committed immediately
    so it survives even if the enclosing request later fails.
    """
    pkg = canonicalize_name(package)
    existing = session.execute(
        select(DocIngestJob).where(
            DocIngestJob.package_name == pkg,
            DocIngestJob.source_url == url,
            DocIngestJob.status == DocIngestJobStatus.PENDING.value,
        )
    ).scalars().first()
    if existing is not None:
        return existing
    job = DocIngestJob(
        package_name=pkg,
        source_url=url,
        source_title=title or "",
        source_version_spec=source_version_spec or "",
        target_version_spec=target_version_spec or "",
        trust_level=trust_level or "community",
        snapshot_hash=snapshot_hash or "",
        status=DocIngestJobStatus.PENDING.value,
        reason=reason or "",
        discovered=json.dumps(discovered or [], ensure_ascii=False),
    )
    session.add(job)
    session.flush()
    session.commit()
    return job


def pending_jobs(session: Session, *, limit: int | None = None) -> list[DocIngestJob]:
    """Return pending backfill jobs, oldest first."""
    q = select(DocIngestJob).where(
        DocIngestJob.status == DocIngestJobStatus.PENDING.value
    ).order_by(DocIngestJob.created_at)
    if limit is not None:
        q = q.limit(limit)
    return list(session.execute(q).scalars().all())


def get_job(session: Session, job_id: str) -> DocIngestJob | None:
    return cast("DocIngestJob | None", session.get(DocIngestJob, job_id))
