"""Vector index over doc chunks, backed by ``sqlite-vec``.

The index is *optional*. When the ``sqlite-vec`` extension cannot be loaded on
the current platform, or no embedding backend is configured, the index simply
reports ``available() == False`` and the retrieval layer falls back to FTS5-only
(see :mod:`upgradelens.docs.retrieval`). A missing vector index is never treated
as an error anywhere in the pipeline.

The concrete table layout (a ``vec0`` virtual table) and the ``sqlite-vec`` SQL
are isolated here so the rest of the codebase never imports the extension
directly. Two invariants are enforced:

* the vector index is fully rebuildable from ``doc_chunks`` -- call
  :meth:`SqliteVecIndex.rebuild` after (re)ingesting documentation;
* the model that built the vectors is recorded in ``embedding_meta`` so a
  dimension/model mismatch triggers a rebuild rather than silently mixing
  embeddings from different models.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from upgradelens.db import models

_VECTOR_TABLE = "doc_embeddings"


class VectorIndexUnavailable(RuntimeError):
    """Raised when sqlite-vec cannot be loaded on the current platform."""


def sqlite_vec_available() -> bool:
    """True when the ``sqlite-vec`` extension can be imported and loaded."""
    try:
        import sqlite_vec  # noqa: F401
    except Exception:  # pragma: no cover - platform dependent
        return False
    return True


def _load_extension(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)


def serialize_vector(vector: list[float]) -> bytes:
    """Pack a float vector into the little-endian blob ``sqlite-vec`` expects."""
    import sqlite_vec

    packed: bytes = sqlite_vec.serialize_float32(vector)
    return packed


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Anything that turns text into a fixed-dimension float vector.

    Implementations must be *real* semantic models -- a hash or random vector
    is forbidden because it would poison the recall metrics and hide
    regressions in hybrid retrieval. ``available`` lets the pipeline skip the
    vector path entirely (and fall back to FTS5-only) when no model is wired up.
    """

    model: str
    dimension: int

    def available(self) -> bool: ...

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


class VectorMatch:
    """A single nearest-neighbour hit from the vector index."""

    __slots__ = ("chunk_id", "distance")

    def __init__(self, chunk_id: int, distance: float) -> None:
        self.chunk_id = chunk_id
        self.distance = distance


class VectorIndex:
    """Abstract vector store for doc chunk embeddings."""

    def available(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def upsert(self, chunk_id: int, vector: list[float]) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete(self, chunk_id: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def search(self, vector: list[float], top_k: int) -> list[VectorMatch]:  # pragma: no cover
        raise NotImplementedError

    def rebuild(self, session: Session, backend: EmbeddingBackend) -> int:  # pragma: no cover
        raise NotImplementedError


class SqliteVecIndex(VectorIndex):
    """``vec0``-backed implementation of :class:`VectorIndex`.

    Detection is explicit: construction either yields a ready index or raises
    :class:`VectorIndexUnavailable`. A single raw sqlite3 connection (kept
    independent of the SQLAlchemy ``session``) owns the virtual table, so vector
    writes never tangle with SQLAlchemy's transaction state; both connections
    point at the same SQLite file and sqlite serialises the writes.
    """

    def __init__(self, session: Session, dimension: int) -> None:
        if dimension <= 0:
            raise VectorIndexUnavailable("embedding dimension must be positive")
        if not sqlite_vec_available():
            raise VectorIndexUnavailable("sqlite-vec extension is not available")
        self._dimension = dimension
        self._conn = self._open_connection(session)
        try:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VECTOR_TABLE} "
                f"USING vec0(embedding float[{dimension}])"
            )
            self._conn.commit()
        except Exception as exc:  # pragma: no cover - platform dependent
            self._conn.close()
            raise VectorIndexUnavailable(f"failed to create vec0 table: {exc}") from exc

    @staticmethod
    def _open_connection(session: Session) -> sqlite3.Connection:
        bind = session.get_bind()
        engine = bind if isinstance(bind, Engine) else bind.engine
        path = engine.url.database
        if path in (":memory:", "", None):
            raise VectorIndexUnavailable("in-memory databases cannot host a persistent vec index")
        conn = sqlite3.connect(path)
        _load_extension(conn)
        return conn

    def available(self) -> bool:
        return True

    def upsert(self, chunk_id: int, vector: list[float]) -> None:
        blob = serialize_vector(vector)
        # vec0 has no upsert; delete-then-insert is the documented pattern.
        self._conn.execute(f"DELETE FROM {_VECTOR_TABLE} WHERE rowid = ?", (chunk_id,))
        self._conn.execute(
            f"INSERT INTO {_VECTOR_TABLE}(rowid, embedding) VALUES(?, ?)",
            (chunk_id, blob),
        )
        self._conn.commit()

    def delete(self, chunk_id: int) -> None:
        self._conn.execute(f"DELETE FROM {_VECTOR_TABLE} WHERE rowid = ?", (chunk_id,))
        self._conn.commit()

    def search(self, vector: list[float], top_k: int) -> list[VectorMatch]:
        blob = serialize_vector(vector)
        rows = self._conn.execute(
            f"SELECT rowid, distance FROM {_VECTOR_TABLE} "
            f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, top_k),
        ).fetchall()
        return [VectorMatch(chunk_id=int(r[0]), distance=float(r[1])) for r in rows]

    def rebuild(self, session: Session, backend: EmbeddingBackend) -> int:
        """Rebuild the whole index from ``doc_chunks`` using ``backend``.

        Returns the number of chunks indexed. The embedding model/version is
        recorded in ``embedding_meta`` so a later mismatch forces another
        rebuild instead of mixing vectors from different models.
        """
        rows = session.execute(select(models.DocChunkRow)).scalars().all()
        if not rows:
            self._clear()
            self._record_meta(session, backend)
            return 0
        texts = [f"{row.title}\n{row.content}" for row in rows]
        vectors = backend.embed(texts)
        if vectors is None:
            return 0
        self._clear()
        for row, vec in zip(rows, vectors, strict=False):
            self.upsert(row.id, vec)
        self._record_meta(session, backend)
        return len(rows)

    def _clear(self) -> None:
        self._conn.execute(f"DELETE FROM {_VECTOR_TABLE}")
        self._conn.commit()

    def _record_meta(self, session: Session, backend: EmbeddingBackend) -> None:
        meta = session.get(models.EmbeddingMeta, 1)
        if meta is None:
            meta = models.EmbeddingMeta(id=1)
            session.add(meta)
        meta.model = getattr(backend, "model", "")
        meta.dimension = self._dimension
        meta.version = getattr(backend, "version", "")
        session.commit()
