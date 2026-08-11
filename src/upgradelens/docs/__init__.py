"""Documentation pipeline: cleaning, chunking, ingestion and retrieval (stage 4)."""

# S17: online-discovered sources are enqueued and later re-ingested into the corpus.
from upgradelens.db.models import DocIngestJob, DocIngestJobStatus  # noqa: F401
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
from upgradelens.docs.jobs import enqueue_ingest_job, get_job, pending_jobs  # noqa: F401
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
from upgradelens.docs.worker import process_pending_jobs, run_ingest_job  # noqa: F401

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
    "DocIngestJob",
    "DocIngestJobStatus",
    "enqueue_ingest_job",
    "get_job",
    "pending_jobs",
    "process_pending_jobs",
    "run_ingest_job",
]
