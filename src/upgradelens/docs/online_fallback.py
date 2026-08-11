"""Online fallback when the shared local corpus misses (S16).

The shared RAG corpus is the default source of truth. When it cannot serve a
request (see :mod:`upgradelens.docs.rag_miss`), S16 may, *in live mode only*,
discover official documentation online, fetch it through the restricted
fetcher, chunk it in memory and retrieve against the current query — feeding
the result through the *same* verifier as local evidence. The result is served
for the **current request only**; S17 is responsible for persisting anything
worth keeping back into the corpus.

Safety properties (acceptance criteria):

* Every fetch goes through :class:`upgradelens.tools.fetcher.RestrictedFetcher`
  (SSRF / redirect / size / timeout guards — reuse, don't reimplement).
* Online evidence carries ``provenance = "online_fallback"`` and is never
  auto-trusted to ``official`` unless the URL host is in the official allowlist,
  so a single non-official source cannot become a fully verified finding.
* All failures degrade gracefully: the function returns whatever it found (often
  nothing) and records a trace event — it never raises into the agent loop.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from upgradelens.docs.chunking import chunk_markdown
from upgradelens.docs.rag_miss import RagMissReason
from upgradelens.domain.doc_evidence import DocChunk, DocEvidence, RetrievalRun
from upgradelens.tools.errors import (
    FetchTimeoutError,
    HttpError,
    OutOfNetworkError,
    TooLargeError,
    ToolError,
    TooManyRedirectsError,
)
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.trace import ToolTrace

# Hosts we are willing to treat as *official* project documentation when reached
# through online discovery. Anything else is "community" at best.
OFFICIAL_DOC_HOSTS = frozenset(
    {
        "pypi.org",
        "docs.python.org",
        "readthedocs.org",
        "readthedocs.io",
        "github.com",
    }
)

# Every exception the restricted fetcher may raise on a blocked / failed fetch.
FETCH_EXCEPTIONS = (
    OutOfNetworkError,
    HttpError,
    TooLargeError,
    FetchTimeoutError,
    TooManyRedirectsError,
    ToolError,
)

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class DiscoveredSource:
    url: str
    title: str
    kind: str = "docs"
    trust_hint: str = "community"
    source: str = ""

    def __post_init__(self) -> None:
        host = (urlsplit(self.url).hostname or "").lower()
        self.source = host
        if not self.trust_hint or self.trust_hint == "community":
            if any(host == h or host.endswith("." + h) for h in OFFICIAL_DOC_HOSTS):
                self.trust_hint = "official"


@runtime_checkable
class DocDiscoveryProvider(Protocol):
    """A keyless way to turn a package name into candidate doc URLs."""

    def discover(self, package: str, *, fetcher: RestrictedFetcher) -> list[DiscoveredSource]:
        ...


class PypiJsonProvider:
    """Discover doc/homepage URLs from the public PyPI JSON API (no auth)."""

    def discover(self, package: str, *, fetcher: RestrictedFetcher) -> list[DiscoveredSource]:
        url = f"https://pypi.org/pypi/{package}/json"
        try:
            content = fetcher.fetch(url)
        except FETCH_EXCEPTIONS:
            return []
        try:
            data = json.loads(content.content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
        info = data.get("info", {}) or {}
        found: list[DiscoveredSource] = []
        seen: set[str] = set()
        project_urls = info.get("project_urls") or {}
        for label, candidate in project_urls.items():
            if not candidate:
                continue
            found.append(
                DiscoveredSource(
                    url=candidate,
                    title=f"{package} — {label}",
                    kind=str(label).lower(),
                )
            )
        for candidate in (info.get("docs_url"), info.get("home_page")):
            if candidate:
                found.append(DiscoveredSource(url=candidate, title=f"{package} 文档", kind="docs"))
        out: list[DiscoveredSource] = []
        for src in found:
            key = src.url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(src)
        return out


DEFAULT_DISCOVERY_PROVIDERS: list[DocDiscoveryProvider] = [PypiJsonProvider()]


def discover_sources(
    package: str,
    *,
    fetcher: RestrictedFetcher,
    top_n: int = 6,
    providers: list[DocDiscoveryProvider] | None = None,
) -> list[DiscoveredSource]:
    """Run the discovery providers and return de-duplicated candidate sources.

    Official-host sources are sorted first so the fetcher prefers them.
    """
    providers = providers or DEFAULT_DISCOVERY_PROVIDERS
    out: list[DiscoveredSource] = []
    seen: set[str] = set()
    for provider in providers:
        for src in provider.discover(package, fetcher=fetcher):
            key = src.url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(src)
    out.sort(key=lambda s: (0 if s.trust_hint == "official" else 1, s.url))
    return out[:top_n]


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text)


def _payload_to_text(source: DiscoveredSource, text: str, content_type: str) -> str:
    ctype = content_type.lower()
    if "json" in ctype:
        try:
            data = json.loads(text)
            info = data.get("info", data) if isinstance(data, dict) else {}
            parts = [str(v) for v in info.values() if isinstance(v, (str, int, float))]
            return "\n".join(parts)
        except json.JSONDecodeError:
            return text
    if "<" in text and ("text/html" in ctype or text.lstrip().lower().startswith("<!doctype")):
        return _strip_html(text)
    return text


def fetch_and_chunk(
    source: DiscoveredSource, fetcher: RestrictedFetcher, *, source_id: str
) -> list[DocChunk]:
    """Fetch a discovered source and turn it into in-memory chunks."""
    try:
        content = fetcher.fetch(source.url)
    except FETCH_EXCEPTIONS:
        return []
    text = content.content.decode("utf-8", errors="replace")
    text = _payload_to_text(source, text, content.content_type)
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if not text.strip():
        return []
    return chunk_markdown(text, source_id)


def _score_chunk(chunk: DocChunk, terms: list[str]) -> float:
    lowered = chunk.content.lower()
    return float(sum(lowered.count(t) for t in terms))


def temporary_retrieve(
    package: str,
    source_version: str,
    target_version: str,
    user_intent: str,
    code_symbols: list[str],
    chunks: list[DocChunk],
    *,
    top_k: int = 4,
) -> list[DocEvidence]:
    """Lexically retrieve the best temporary chunks and wrap them as evidence.

    The scoring is intentionally simple and deterministic (no model call): it
    counts query-term hits so the result is reproducible and testable.
    """
    if not chunks:
        return []
    target_major = target_version.split(".")[0]
    terms: list[str] = [t.lower() for t in (user_intent or "").split()]
    terms += [s.lower() for s in code_symbols]
    terms += ["breaking", "change", "migration", "upgrade", f"v{target_major}", target_major]
    terms = [t for t in terms if t]
    scored = sorted(chunks, key=lambda c: _score_chunk(c, terms), reverse=True)
    picked = [c for c in scored if _score_chunk(c, terms) > 0][:top_k]
    evidence: list[DocEvidence] = []
    for chunk in picked:
        snippet = chunk.content.strip().replace("\n", " ")[:280]
        evidence.append(
            DocEvidence(
                evidence_id=f"online-{uuid.uuid4().hex[:12]}",
                source_id=chunk.source_id,
                url=chunk.source_id,
                title=chunk.title,
                chunk_title=chunk.title,
                heading_path=chunk.heading_path,
                snapshot_hash="",
                snippet=snippet,
                score=_score_chunk(chunk, terms),
                matched_query=user_intent,
                package_name=package,
                source_version_spec=source_version,
                target_version_spec=target_version,
                trust_level="community",
                chunk_content_hash=chunk.content_hash,
                provenance="online_fallback",
            )
        )
    return evidence


@dataclass
class OnlineFallbackResult:
    runs: list[RetrievalRun] = None  # type: ignore[assignment]
    evidence: list[DocEvidence] = None  # type: ignore[assignment]
    status: str = "ok"  # ok | partial | failed | skipped
    fetched: int = 0
    discovered: int = 0
    reason: str = ""
    #: The sources actually discovered for this package -- consumed by S17 to
    #: enqueue background corpus-backfill jobs. Empty when nothing was fetched.
    sources: list[DiscoveredSource] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.runs is None:
            self.runs = []
        if self.evidence is None:
            self.evidence = []
        if self.sources is None:
            self.sources = []


def run_online_fallback(
    package: str,
    source_version: str,
    target_version: str,
    user_intent: str,
    code_symbols: list[str],
    *,
    fetcher: RestrictedFetcher,
    network: str = "online_fallback",
    trace: ToolTrace | None = None,
    miss_reason: RagMissReason = RagMissReason.QUERY_NO_HIT,
    plan_step_id: str = "",
    top_k: int = 4,
    fetch_cap: int = 3,
) -> OnlineFallbackResult:
    """Discover -> fetch -> chunk -> temporary retrieve, emitting trace events.

    Returns whatever evidence was recovered. On any error it degrades to an
    empty result with a recorded trace event; it never raises into the caller.
    """
    from upgradelens.config import NetworkMode

    if NetworkMode(network) == NetworkMode.OFFLINE:
        return OnlineFallbackResult(status="skipped", reason="offline")

    if trace is not None:
        trace.record(
            tool="rag_miss",
            target=package,
            status="ok",
            params={"reason": miss_reason.value},
            plan_step_id=plan_step_id,
        )

    sources = discover_sources(package, fetcher=fetcher)
    if trace is not None:
        trace.record(
            tool="discover",
            target=package,
            status="ok" if sources else "error",
            params={"urls": [s.url for s in sources]},
            plan_step_id=plan_step_id,
        )
    if not sources:
        return OnlineFallbackResult(status="failed", discovered=0, reason="no_sources")

    chunks: list[DocChunk] = []
    fetched = 0
    for src in sources[:fetch_cap]:
        try:
            src_chunks = fetch_and_chunk(src, fetcher, source_id=src.url)
        except FETCH_EXCEPTIONS as exc:
            if trace is not None:
                trace.record(
                    tool="fetch",
                    target=src.url,
                    status="error",
                    error=str(exc),
                    plan_step_id=plan_step_id,
                )
            continue
        if trace is not None:
            trace.record(
                tool="fetch",
                target=src.url,
                status="ok",
                bytes_=sum(len(c.content.encode("utf-8")) for c in src_chunks),
                params={"chunks": len(src_chunks)},
                plan_step_id=plan_step_id,
            )
        fetched += 1
        chunks.extend(src_chunks)

    evidence = temporary_retrieve(
        package, source_version, target_version, user_intent, code_symbols, chunks, top_k=top_k
    )
    if trace is not None:
        trace.record(
            tool="temporary_retrieve",
            target=package,
            status="ok" if evidence else "error",
            params={"count": len(evidence), "evidence_ids": [e.evidence_id for e in evidence]},
            plan_step_id=plan_step_id,
        )

    status = "ok" if evidence else ("partial" if fetched else "failed")
    runs: list[RetrievalRun] = []
    if evidence:
        runs.append(
            RetrievalRun(
                run_id=f"online-{package}-{uuid.uuid4().hex[:8]}",
                source_id=f"online:{package}",
                query=user_intent,
                matched_chunk_ids=[],
                top_doc_evidence=evidence,
            )
        )
        if trace is not None:
            trace.record(
                tool="online_supplement",
                target=package,
                status="ok",
                params={"evidence_ids": [e.evidence_id for e in evidence]},
                plan_step_id=plan_step_id,
                evidence_ids=[e.evidence_id for e in evidence],
            )
    return OnlineFallbackResult(
        runs=runs,
        evidence=evidence,
        status=status,
        fetched=fetched,
        discovered=len(sources),
        sources=sources,
    )
