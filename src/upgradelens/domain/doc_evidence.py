"""Domain models for document evidence and keyword RAG (stage 4).

All models are immutable (``frozen=True``) and forbid extra fields, matching the
conventions established in :mod:`upgradelens.domain.code_evidence` and
:mod:`upgradelens.domain.skill`.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field


def _stable_sha256(text: str) -> str:
    """Return a stable hex sha256 digest for ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _frozen() -> ConfigDict:
    """Shared immutable model configuration."""
    return ConfigDict(frozen=True, extra="forbid")


class DocChunk(BaseModel):
    """A heading-aware slice of a documentation source.

    ``heading_path`` is the ordered list of heading titles from the document
    root down to this chunk. ``content_hash`` is a sha256 of ``content`` so the
    ingestion pipeline can deduplicate and detect changes.
    """

    model_config = _frozen()

    source_id: str
    title: str
    heading_path: list[str] = Field(default_factory=list)
    content: str
    content_hash: str = ""

    def with_hash(self) -> DocChunk:
        """Return a copy with ``content_hash`` populated if it is empty."""
        if self.content_hash:
            return self
        return self.model_copy(update={"content_hash": _stable_sha256(self.content)})


class DocSourceRecord(BaseModel):
    """Lightweight descriptor of an ingested documentation source."""

    model_config = _frozen()

    id: str
    url: str
    title: str
    snapshot_hash: str
    target_version_spec: str = ""
    chunk_count: int = 0


class DocEvidence(BaseModel):
    """A retrieved documentation chunk, linked back to its source.

    Every field needed to cite the evidence is present: ``url``, ``title`` and
    ``snapshot_hash`` identify the snapshot, while ``heading_path`` locates the
    exact section inside it.
    """

    model_config = _frozen()

    source_id: str
    url: str
    title: str
    chunk_title: str
    heading_path: list[str] = Field(default_factory=list)
    snapshot_hash: str
    snippet: str
    score: float
    matched_query: str


class RetrievalRun(BaseModel):
    """The result of one keyword retrieval over a documentation source."""

    model_config = _frozen()

    run_id: str
    source_id: str
    query: str
    matched_chunk_ids: list[int] = Field(default_factory=list)
    top_doc_evidence: list[DocEvidence] = Field(default_factory=list)
    generated_at: str = ""
