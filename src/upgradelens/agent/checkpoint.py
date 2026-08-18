"""Checkpoints, resume and idempotency for multi-agent runs (MA-1B-3).

A :class:`Checkpoint` is a resumable snapshot of a run's outcome. The runtime
persists one per (run_id, step); on a re-dispatch of the *same* run id it can
short-circuit to the stored result instead of recomputing -- that is what makes
a plan idempotent and crash-recoverable.

Two backends ship:

* :class:`MemoryCheckpointStore` -- for tests and ephemeral runs;
* :class:`SQLiteCheckpointStore` -- persists to the shared run store DB so a
  crashed supervisor can resume after restart.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from upgradelens.agent.runner import AgentRunner
from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
    Checkpoint,
    RunId,
    RunStatus,
    TaskEnvelope,
)


class CheckpointStore(ABC):
    """Persistence for :class:`Checkpoint` snapshots."""

    @abstractmethod
    def save(self, cp: Checkpoint) -> None: ...

    @abstractmethod
    def load(self, run_id: RunId, step: str) -> Checkpoint | None: ...

    @abstractmethod
    def latest(self, run_id: RunId) -> Checkpoint | None: ...

    @abstractmethod
    def completed(self, run_id: RunId) -> bool: ...


class MemoryCheckpointStore(CheckpointStore):
    """In-memory checkpoint store (tests / ephemeral runs)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], Checkpoint] = {}
        self._order: list[tuple[str, str]] = []

    def save(self, cp: Checkpoint) -> None:
        key = (cp.run_id, cp.step)
        if key not in self._by_key:
            self._order.append(key)
        self._by_key[key] = cp

    def load(self, run_id: RunId, step: str) -> Checkpoint | None:
        return self._by_key.get((run_id, step))

    def latest(self, run_id: RunId) -> Checkpoint | None:
        for rid, step in reversed(self._order):
            if rid == run_id:
                return self._by_key[(rid, step)]
        return None

    def completed(self, run_id: RunId) -> bool:
        cp = self.latest(run_id)
        return cp is not None and bool(cp.state.get("status") == RunStatus.COMPLETED.value)


class SQLiteCheckpointStore(CheckpointStore):
    """Persist checkpoints in a SQLite table (shared run store DB)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_checkpoints (
                run_id TEXT NOT NULL,
                step TEXT NOT NULL,
                state_json TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, step)
            )
            """
        )
        conn.commit()

    def save(self, cp: Checkpoint) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO agent_checkpoints "
            "(run_id, step, state_json, state_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                cp.run_id,
                cp.step,
                json.dumps(cp.state, ensure_ascii=False),
                cp.state_hash,
                cp.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def load(self, run_id: RunId, step: str) -> Checkpoint | None:
        row = self.conn.execute(
            "SELECT state_json, state_hash, created_at FROM agent_checkpoints "
            "WHERE run_id = ? AND step = ?",
            (run_id, step),
        ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            run_id=run_id,
            step=step,
            state=json.loads(row[0]),
            state_hash=row[1],
            created_at=_parse_dt(row[2]),
        )

    def latest(self, run_id: RunId) -> Checkpoint | None:
        row = self.conn.execute(
            "SELECT step, state_json, state_hash, created_at FROM agent_checkpoints "
            "WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            run_id=run_id,
            step=row[0],
            state=json.loads(row[1]),
            state_hash=row[2],
            created_at=_parse_dt(row[3]),
        )

    def completed(self, run_id: RunId) -> bool:
        cp = self.latest(run_id)
        return cp is not None and cp.state.get("status") == RunStatus.COMPLETED.value


def _parse_dt(value: str) -> Any:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now()


# ---------------------------------------------------------------------------
# Resume / idempotency helper
# ---------------------------------------------------------------------------


def run_with_checkpoint(
    runner: AgentRunner,
    kind: AgentKind,
    ctx: AgentRunContext,
    task: TaskEnvelope,
    store: CheckpointStore,
    *,
    step: str = "main",
) -> AgentResult:
    """Run an agent, resuming from a completed checkpoint if one exists.

    If a *completed* checkpoint for ``(ctx.run_id, step)`` already exists, the
    stored result is returned without recomputing -- this is the idempotency /
    crash-recovery guarantee. Otherwise the agent runs and its outcome is
    checkpointed.
    """
    existing = store.load(ctx.run_id, step)
    if existing is not None and existing.state.get("status") == RunStatus.COMPLETED.value:
        return _result_from_checkpoint(ctx, kind, existing)

    result = runner.run(kind, ctx, task)

    cp = Checkpoint(
        run_id=ctx.run_id,
        step=step,
        state={
            "status": result.status.value,
            "summary": result.summary,
            "finding_count": result.finding_count,
            "agent_id": result.agent_id,
        },
        state_hash=_hash_state(result),
    )
    store.save(cp)
    return result


def _result_from_checkpoint(
    ctx: AgentRunContext, kind: AgentKind, cp: Checkpoint
) -> AgentResult:
    return AgentResult(
        run_id=ctx.run_id,
        parent_run_id=ctx.parent_run_id,
        agent_id=str(cp.state.get("agent_id", kind.value)),
        kind=kind,
        status=RunStatus.COMPLETED,
        summary=str(cp.state.get("summary", "")),
        findings=[],
        notes={"resumed_from_checkpoint": True},
    )


def _hash_state(result: AgentResult) -> str:
    import hashlib

    blob = f"{result.status.value}|{result.summary}|{result.finding_count}".encode()
    return hashlib.sha1(blob).hexdigest()[:16]


__all__ = [
    "CheckpointStore",
    "MemoryCheckpointStore",
    "SQLiteCheckpointStore",
    "run_with_checkpoint",
]
