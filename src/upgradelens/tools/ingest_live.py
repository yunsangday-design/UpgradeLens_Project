"""Ingest live (fetched) documents into the SQLite evidence store (stage 7).

Reuses the stage 4 cleaning/chunking/FTS pipeline via
:func:`upgradelens.docs.ingest.persist_source_text`. The live-specific parts
are: (1) every source is fetched through the traced, restricted fetcher, and
(2) each source's trust level is *inferred from its URL* rather than trusting
the declaration -- a fetched URL can drift from what the author assumed.

Since S6 live documents are tagged with their package so they land in the same
shared corpus as manifest-ingested ones; callers pass the dependency they are
fetching for.
"""

from __future__ import annotations

from packaging.utils import canonicalize_name
from sqlalchemy.orm import Session

from upgradelens.docs.ingest import persist_source_text
from upgradelens.domain.doc_evidence import DocSourceRecord
from upgradelens.domain.doc_source_spec import DocSourceSpec
from upgradelens.domain.skill import DocSource
from upgradelens.tools.errors import ToolError
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.pypi import ChangelogEntry
from upgradelens.tools.trust import infer_trust


def ingest_live_source(
    session: Session,
    source: DocSource,
    fetcher: RestrictedFetcher,
    *,
    refresh: bool = False,
    package_name: str = "",
    source_version_spec: str = "",
) -> DocSourceRecord | None:
    """Fetch one declared source live and persist it into the shared corpus.

    Returns ``None`` (and leaves the trace recording the failure) if the fetch
    fails -- a single dead URL must not abort the whole run.
    """
    if not source.url:
        return None
    try:
        result = fetcher.fetch(source.url, refresh=refresh)
    except ToolError:
        return None
    raw = result.content.decode("utf-8", "replace")
    spec = DocSourceSpec(
        id=source.id,
        package_name=package_name,
        url=source.url,
        title=source.id,
        source_type=source.source_type,
        trust_level=source.trust_level,
        source_version_spec=source_version_spec,
        target_version_spec=source.target_version_spec or "",
        fetch_strategy=source.fetch_strategy,
        parse_strategy=source.parse_strategy,
    )
    trust = infer_trust(source.url)
    return persist_source_text(session, spec, raw, snapshot_path=source.url, trust_level=trust)


def _format_changelog(dependency: str, entries: list[ChangelogEntry]) -> str:
    lines = [f"# {dependency} release changelog (source: PyPI)", ""]
    for entry in entries[:100]:
        flag = " (yanked)" if entry.is_yanked else (" (prerelease)" if entry.is_prerelease else "")
        lines.append(f"## {entry.version}{flag}")
        if entry.released:
            lines.append(f"- released: {entry.released}")
        lines.append(f"- url: {entry.url}")
        lines.append("")
    return "\n".join(lines)


def ingest_pypi_changelog(
    session: Session,
    dependency: str,
    entries: list[ChangelogEntry],
    *,
    target_version_spec: str = "",
    source_version_spec: str = "",
) -> DocSourceRecord:
    """Persist a PyPI changelog as an ``official`` changelog doc source."""
    spec = DocSourceSpec(
        id=f"{dependency}:pypi-changelog",
        package_name=canonicalize_name(dependency),
        url=f"https://pypi.org/pypi/{dependency}/json",
        title=f"{dependency} release changelog",
        source_type="changelog",
        trust_level="official",
        source_version_spec=source_version_spec,
        target_version_spec=target_version_spec,
        fetch_strategy="html",
    )
    raw = _format_changelog(dependency, entries)
    return persist_source_text(session, spec, raw, snapshot_path=spec.url, trust_level="official")
