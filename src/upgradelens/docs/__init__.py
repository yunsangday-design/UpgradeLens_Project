"""Documentation pipeline: cleaning, chunking, ingestion and retrieval (stage 4)."""

from upgradelens.docs.chunking import chunk_markdown
from upgradelens.docs.cleaning import clean_document, clean_html, clean_markdown
from upgradelens.docs.ingest import (
    ingest_corpus,
    ingest_manifest,
    ingest_manifest_file,
    ingest_skill,
    ingest_skill_source,
    ingest_source_spec,
    iter_sources_for_package,
)
from upgradelens.docs.retrieval import (
    build_fts_query,
    retrieve,
    retrieve_skill_evidence,
)
from upgradelens.docs.source_manifest import (
    DocSourceManifestError,
    discover_manifests,
    load_source_manifest,
)

__all__ = [
    "DocSourceManifestError",
    "clean_document",
    "clean_html",
    "clean_markdown",
    "chunk_markdown",
    "discover_manifests",
    "load_source_manifest",
    "ingest_corpus",
    "ingest_manifest",
    "ingest_manifest_file",
    "ingest_source_spec",
    "ingest_skill",
    "ingest_skill_source",
    "iter_sources_for_package",
    "build_fts_query",
    "retrieve",
    "retrieve_skill_evidence",
]
