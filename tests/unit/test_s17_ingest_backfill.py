"""Tests for S17: online-discovered sources are re-ingested into the corpus.

Covers the three moving parts:

* enqueue (``docs/jobs.py``) -- de-duplicated, committed immediately;
* worker (``docs/worker.py``) -- re-fetches through the guarded fetcher and
  persists into the shared corpus, honouring the NetworkMode policy;
* registry gate (``tools/registry.py``) -- live + online_fallback enqueues,
  fake/offline never does.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
from unittest import mock

from sqlalchemy.orm import Session

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.db.models import DocIngestJob, DocIngestJobStatus
from upgradelens.docs.ingest import iter_sources_for_package
from upgradelens.docs.jobs import enqueue_ingest_job, get_job, pending_jobs
from upgradelens.docs.online_fallback import DiscoveredSource, OnlineFallbackResult
from upgradelens.docs.retrieval import retrieve
from upgradelens.docs.worker import run_ingest_job
from upgradelens.llm.gateway import ModelMode
from upgradelens.tools.errors import OutOfNetworkError
from upgradelens.tools.fetcher import FetchResult
from upgradelens.tools.registry import (
    RetrieveForPackageInput,
    ToolContext,
    _handle_retrieve_for_package,
)
from upgradelens.tools.trace import ToolTrace


class _FakeFetcher:
    """Stand-in for RestrictedFetcher that returns canned bytes (or raises)."""

    def __init__(self, content: bytes = b"", raise_on_fetch: Exception | None = None) -> None:
        self._content = content
        self._raise = raise_on_fetch
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self._raise is not None:
            raise self._raise
        return FetchResult(
            url=url,
            final_url=url,
            status=200,
            content=self._content,
            content_type="text/html",
            etag=None,
        )


DOCS_TEXT = (
    b"# Requests\n\n"
    b"The Requests library lets you send HTTP/1.1 requests. "
    b"Upgrade notes: sessions changed in 2.0. Use ``requests.Session``.\n"
)


class TestEnqueueIngestJob:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.sqlite")
        self.engine = engine_for(self.db)
        init_db(self.engine)
        self.engine = engine_for(self.db)

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self) -> Session:
        return session_for(self.engine)()

    def test_enqueue_creates_pending_row(self) -> None:
        session = self._session()
        try:
            job = enqueue_ingest_job(
                session,
                package="requests",
                url="https://requests.readthedocs.io/en/stable/",
                title="Requests",
                source_version_spec="",
                target_version_spec="",
                trust_level="community",
                discovered=[],
            )
            assert job.status == DocIngestJobStatus.PENDING.value
            assert job.package_name == "requests"
            assert get_job(session, job.id) is not None
        finally:
            session.close()

    def test_enqueue_dedup_pending(self) -> None:
        session = self._session()
        try:
            first = enqueue_ingest_job(
                session,
                package="Requests",
                url="https://requests.readthedocs.io/en/stable/",
                title="Requests",
                source_version_spec="",
                target_version_spec="",
                trust_level="community",
                discovered=[],
            )
            # Same (package, url) again must return the existing pending job.
            second = enqueue_ingest_job(
                session,
                package="requests",
                url="https://requests.readthedocs.io/en/stable/",
                title="Requests",
                source_version_spec="",
                target_version_spec="",
                trust_level="community",
                discovered=[],
            )
            assert first.id == second.id
            assert len(pending_jobs(session)) == 1
        finally:
            session.close()


class TestWorkerPersistsToCorpus:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.sqlite")
        self.engine = engine_for(self.db)
        init_db(self.engine)
        self.engine = engine_for(self.db)

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _enqueue(self, session: Session, **kwargs) -> DocIngestJob:
        return enqueue_ingest_job(
            session,
            package="requests",
            url="https://requests.readthedocs.io/en/stable/",
            title="Requests",
            source_version_spec="",
            target_version_spec="",
            trust_level="community",
            discovered=[],
            **kwargs,
        )

    def test_worker_persists_and_local_hit(self) -> None:
        """A processed job lands in the corpus and is retrievable next time."""
        session = session_for(self.engine)()
        try:
            job = self._enqueue(session)
            status = run_ingest_job(
                job,
                session=session,
                fetcher=_FakeFetcher(content=DOCS_TEXT),
                network="online_fallback",
            )
            assert status == DocIngestJobStatus.DONE.value
            assert job.chunks_collected > 0

            # The source is now part of the local corpus for this package.
            sources = iter_sources_for_package(session, "requests")
            assert any(s.package_name == "requests" for s in sources)
            source_id = next(s.id for s in sources if s.package_name == "requests")

            # And a second retrieval finds it locally (no network needed).
            run = retrieve(session, source_id, "requests session upgrade")
            assert run.top_doc_evidence is not None, "ingested source must be retrivable locally"
        finally:
            session.close()

    def test_worker_offline_skips(self) -> None:
        session = session_for(self.engine)()
        try:
            job = self._enqueue(session)
            fetcher = _FakeFetcher(content=DOCS_TEXT)
            status = run_ingest_job(job, session=session, fetcher=fetcher, network="offline")
            assert status == DocIngestJobStatus.SKIPPED.value
            assert fetcher.calls == []  # never fetched in offline mode
            # Nothing was persisted into the corpus.
            assert iter_sources_for_package(session, "requests") == []
        finally:
            session.close()

    def test_worker_fetch_failure_marks_failed(self) -> None:
        session = session_for(self.engine)()
        try:
            job = self._enqueue(session)
            fetcher = _FakeFetcher(raise_on_fetch=OutOfNetworkError("blocked"))
            status = run_ingest_job(
                job, session=session, fetcher=fetcher, network="online_fallback"
            )
            assert status == DocIngestJobStatus.FAILED.value
            assert "fetch failed" in (job.error or "")
        finally:
            session.close()


class TestRegistryEnqueueGate:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.sqlite")
        self.engine = engine_for(self.db)
        init_db(self.engine)
        self.engine = engine_for(self.db)

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, mode: ModelMode) -> ToolContext:
        return ToolContext(
            gateway=types.SimpleNamespace(mode=mode),
            trace=ToolTrace(),
            embedding=None,
        )

    def _args(self) -> RetrieveForPackageInput:
        return RetrieveForPackageInput(
            package="requests",
            source_version="2.0.0",
            target_version="2.31.0",
            user_intent="",
            code_symbols=[],
            db=self.db,
            source_id="",
            curated_queries=None,
            top_k=5,
        )

    def test_live_enqueues_when_fetched(self) -> None:
        fb = OnlineFallbackResult(
            runs=[],
            evidence=[],
            status="ok",
            fetched=2,
            discovered=1,
            sources=[
                DiscoveredSource(url="https://requests.readthedocs.io/en/stable/", title="Requests")
            ],
        )
        ctx = self._ctx(ModelMode.LIVE)
        with mock.patch(
            "upgradelens.tools.registry.run_online_fallback", return_value=fb
        ), mock.patch(
            "upgradelens.tools.registry._retrieve_for_package", return_value=[]
        ):
            _handle_retrieve_for_package(self._args(), ctx)
        ctx.close()

        session = session_for(self.engine)()
        try:
            jobs = pending_jobs(session)
            assert len(jobs) == 1
            assert jobs[0].package_name == "requests"
            assert jobs[0].status == DocIngestJobStatus.PENDING.value
            events = [e for e in ctx.trace.events if e.tool == "ingest_enqueue"]
            assert events, "registry must record an ingest_enqueue trace event"
        finally:
            session.close()

    def test_fake_does_not_enqueue(self) -> None:
        ctx = self._ctx(ModelMode.FAKE)
        with mock.patch(
            "upgradelens.tools.registry.run_online_fallback"
        ) as mock_fb, mock.patch(
            "upgradelens.tools.registry._retrieve_for_package", return_value=[]
        ):
            _handle_retrieve_for_package(self._args(), ctx)
        ctx.close()
        mock_fb.assert_not_called()

        session = session_for(self.engine)()
        try:
            assert pending_jobs(session) == []
        finally:
            session.close()

    def test_live_no_fetch_no_enqueue(self) -> None:
        """A live fallback that found nothing to fetch must not enqueue."""
        fb = OnlineFallbackResult(
            runs=[], evidence=[], status="failed", fetched=0, discovered=0, sources=[]
        )
        ctx = self._ctx(ModelMode.LIVE)
        with mock.patch(
            "upgradelens.tools.registry.run_online_fallback", return_value=fb
        ), mock.patch(
            "upgradelens.tools.registry._retrieve_for_package", return_value=[]
        ):
            _handle_retrieve_for_package(self._args(), ctx)
        ctx.close()

        session = session_for(self.engine)()
        try:
            assert pending_jobs(session) == []
        finally:
            session.close()
