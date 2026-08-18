"""Job manager for asynchronous task execution (Step 12 increment E1).

Provides:
- ``JobManager``: thread-safe in-memory job registry with bounded executor.
- ``Job``: state machine (queued → running → succeeded/failed/cancelled).
- ``JobEvent``: incremental events emitted during execution for SSE streaming.

Design decisions:
- ``job_id`` is a random UUID (no collision with artifact ``run_id``).
- Event buffer is a bounded ring buffer per job (default 200 events).
- Executor is a bounded ThreadPoolExecutor (default 2 workers).
- Graceful shutdown: ``stop()`` prevents new submissions and waits for running.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobEvent:
    """One incremental event in a job's lifecycle."""

    id: int
    kind: str  # step_started | step_finished | progress | job_succeeded | job_failed
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Format as a Server-Sent Events message."""
        import json

        lines = [f"id: {self.id}"]
        lines.append(f"event: {self.kind}")
        lines.append(f"data: {json.dumps(self.data, ensure_ascii=False)}")
        return "\n".join(lines) + "\n\n"


@dataclass
class Job:
    """A managed background task."""

    job_id: str
    kind: str  # "scan" | "run"
    params: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    run_id: str | None = None  # Link to artifact run_id (for /api/run jobs)

    # Event ring buffer
    _events: list[JobEvent] = field(default_factory=list, repr=False)
    _event_seq: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _max_events: int = field(default=200, repr=False)

    # Condition for SSE waiters
    _condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock()), repr=False
    )

    def emit(self, kind: str, data: dict[str, Any] | None = None) -> JobEvent:
        """Emit an event (thread-safe). Wakes up SSE waiters."""
        with self._lock:
            self._event_seq += 1
            event = JobEvent(id=self._event_seq, kind=kind, data=data or {})
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]
        with self._condition:
            self._condition.notify_all()
        return event

    def events_since(self, last_id: int = 0) -> list[JobEvent]:
        """Return events with id > last_id."""
        with self._lock:
            return [e for e in self._events if e.id > last_id]

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the job state."""
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_id": self.run_id,
            "event_count": self._event_seq,
        }


class JobManager:
    """Thread-safe job registry with bounded thread pool.

    Usage::

        mgr = JobManager(max_workers=2)
        job = mgr.submit("scan", params={...}, fn=my_scan_function)
        # job.job_id can be returned to client as 202
        # GET /api/jobs/{job_id} → job.snapshot()
        # GET /api/jobs/{job_id}/events → SSE stream
        mgr.stop()  # graceful shutdown
    """

    def __init__(self, max_workers: int = 2, max_events: int = 200) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._max_events = max_events
        self._stopped = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="job-worker"
        )

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        fn: Callable[[Job], Any],
    ) -> Job:
        """Submit a new job. Returns immediately with a Job in QUEUED state.

        ``fn`` receives the Job instance so it can emit events during execution.
        ``fn`` should return the result value; exceptions are caught and stored.
        """
        if self._stopped:
            raise RuntimeError("JobManager is stopped, cannot accept new jobs")

        job_id = uuid.uuid4().hex[:16]
        job = Job(
            job_id=job_id,
            kind=kind,
            params=params,
            _max_events=self._max_events,
        )

        with self._lock:
            self._jobs[job_id] = job

        future = self._executor.submit(self._run_job, job, fn)
        # We don't store the future; the job tracks its own state.
        future.add_done_callback(lambda f: None)  # suppress unhandled exception
        return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, timeout: float = 30.0) -> None:
        """Stop accepting new jobs and wait for running ones to finish."""
        self._stopped = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run_job(self, job: Job, fn: Callable[[Job], Any]) -> None:
        """Execute the job function in a worker thread."""
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.emit("job_started", {"kind": job.kind})
        try:
            result = fn(job)
            job.result = result
            job.status = JobStatus.SUCCEEDED
            job.finished_at = time.time()
            job.emit("job_succeeded", {"elapsed": job.finished_at - job.started_at})
        except Exception as exc:
            job.error = str(exc)
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            job.emit("job_failed", {"error": str(exc)})
