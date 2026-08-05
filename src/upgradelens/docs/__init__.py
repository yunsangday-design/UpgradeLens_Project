"""Documentation pipeline: cleaning, chunking, ingestion and retrieval (stage 4)."""

from upgradelens.docs.chunking import chunk_markdown
from upgradelens.docs.cleaning import clean_document, clean_html, clean_markdown
from upgradelens.docs.ingest import ingest_skill, ingest_skill_source
from upgradelens.docs.retrieval import (
    build_fts_query,
    retrieve,
    retrieve_skill_evidence,
)

__all__ = [
    "clean_document",
    "clean_html",
    "clean_markdown",
    "chunk_markdown",
    "ingest_skill",
    "ingest_skill_source",
    "build_fts_query",
    "retrieve",
    "retrieve_skill_evidence",
]
