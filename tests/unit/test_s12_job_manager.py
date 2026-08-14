"""Tests for step12 increment E1: JobManager + async task infrastructure.

Tests cover:
- Job lifecycle (queued → running → succeeded/failed)
- Event emission and retrieval (events_since)
- SSE formatting
- JobManager submit / get / stop
- Concurrent job execution
"""

from __future__ import annotations

import threading
import time

import pytest

from demo.jobs import Job, JobManager, JobStatus


class TestJobLifecycle:
    """Job state transitions and event emission."""

    def test_initial_state(self):
        job = Job(job_id="test-1", kind="scan")
        assert job.status == JobStatus.QUEUED
        assert not job.is_terminal
        assert job.snapshot()["status"] == "queued"

    def test_emit_event(self):
        job = Job(job_id="test-2", kind="scan")
        event = job.emit("step_started", {"step": "parse"})
        assert event.id == 1
        assert event.kind == "step_started"
        assert event.data == {"step": "parse"}

    def test_events_since(self):
        job = Job(job_id="test-3", kind="scan")
        job.emit("a")
        job.emit("b")
        job.emit("c")
        events = job.events_since(1)
        assert len(events) == 2
        assert events[0].kind == "b"
        assert events[1].kind == "c"

    def test_events_since_zero(self):
        job = Job(job_id="test-4", kind="scan")
        job.emit("x")
        events = job.events_since(0)
        assert len(events) == 1

    def test_event_ring_buffer(self):
        job = Job(job_id="test-5", kind="scan", _max_events=5)
        for i in range(10):
            job.emit("e", {"i": i})
        events = job.events_since(0)
        assert len(events) == 5
        assert events[0].data["i"] == 5  # oldest kept

    def test_terminal_states(self):
        job = Job(job_id="t1", kind="run")
        assert not job.is_terminal
        job.status = JobStatus.SUCCEEDED
        assert job.is_terminal
        job.status = JobStatus.FAILED
        assert job.is_terminal
        job.status = JobStatus.CANCELLED
        assert job.is_terminal

    def test_sse_format(self):
        job = Job(job_id="t2", kind="scan")
        event = job.emit("progress", {"pct": 50})
        sse = event.to_sse()
        assert "id: 1\n" in sse
        assert "event: progress\n" in sse
        assert '"pct": 50' in sse
        assert sse.endswith("\n\n")


class TestJobManager:
    """JobManager submit, get, and execution."""

    def test_submit_and_get(self):
        mgr = JobManager(max_workers=1)
        try:
            job = mgr.submit("scan", {"repo": "/tmp"}, lambda j: "done")
            assert mgr.get(job.job_id) is job
            # Wait for completion
            for _ in range(50):
                if job.is_terminal:
                    break
                time.sleep(0.05)
            assert job.status == JobStatus.SUCCEEDED
            assert job.result == "done"
        finally:
            mgr.stop()

    def test_job_failure(self):
        mgr = JobManager(max_workers=1)
        try:
            def failing(j: Job):
                raise ValueError("boom")

            job = mgr.submit("run", {}, failing)
            for _ in range(50):
                if job.is_terminal:
                    break
                time.sleep(0.05)
            assert job.status == JobStatus.FAILED
            assert "boom" in (job.error or "")
        finally:
            mgr.stop()

    def test_emits_lifecycle_events(self):
        mgr = JobManager(max_workers=1)
        try:
            def work(j: Job):
                j.emit("step_started", {"step": "x"})
                return 42

            job = mgr.submit("run", {}, work)
            for _ in range(50):
                if job.is_terminal:
                    break
                time.sleep(0.05)
            kinds = [e.kind for e in job.events_since(0)]
            assert "job_started" in kinds
            assert "step_started" in kinds
            assert "job_succeeded" in kinds
        finally:
            mgr.stop()

    def test_concurrent_jobs(self):
        mgr = JobManager(max_workers=2)
        try:
            barrier = threading.Barrier(2, timeout=5)
            results = []

            def work(j: Job):
                barrier.wait()
                results.append(j.job_id)
                return j.job_id

            job1 = mgr.submit("a", {}, work)
            job2 = mgr.submit("b", {}, work)
            for _ in range(100):
                if job1.is_terminal and job2.is_terminal:
                    break
                time.sleep(0.05)
            assert job1.status == JobStatus.SUCCEEDED
            assert job2.status == JobStatus.SUCCEEDED
            assert len(results) == 2
        finally:
            mgr.stop()

    def test_get_nonexistent(self):
        mgr = JobManager(max_workers=1)
        try:
            assert mgr.get("nonexistent") is None
        finally:
            mgr.stop()

    def test_stop_prevents_new_submissions(self):
        mgr = JobManager(max_workers=1)
        mgr.stop()
        try:
            mgr.submit("x", {}, lambda j: None)
            pytest.fail("should have raised RuntimeError")
        except RuntimeError:
            pass

    def test_snapshot_fields(self):
        mgr = JobManager(max_workers=1)
        try:
            job = mgr.submit("scan", {"repo": "/foo"}, lambda j: {"items": []})
            for _ in range(50):
                if job.is_terminal:
                    break
                time.sleep(0.05)
            snap = job.snapshot()
            assert snap["job_id"] == job.job_id
            assert snap["kind"] == "scan"
            assert snap["status"] == "succeeded"
            assert snap["result"] == {"items": []}
            assert snap["started_at"] is not None
            assert snap["finished_at"] is not None
            assert snap["event_count"] >= 2  # job_started + job_succeeded
        finally:
            mgr.stop()
