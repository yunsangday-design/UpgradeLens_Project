"""S16: online fallback when the shared local corpus misses.

Covers the acceptance criteria without ever touching the real network:

* structured RAG-miss classification,
* discover -> fetch -> temporary_retrieve producing provenance=online_fallback
  evidence whose trust is never auto-official for non-allowlisted hosts,
* online fallback is gated to live mode only (fake/replay never call out),
* online failure degrades gracefully (no exception, empty result),
* the registry handler triggers the fallback only when local misses + live.

(SSRF / scheme / size / redirect / timeout guards are exercised by the existing
``RestrictedFetcher`` suite; S16 reuses that client rather than reimplementing
it.)
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import TestCase, mock

from upgradelens.docs.online_fallback import (
    DiscoveredSource,
    OnlineFallbackResult,
    discover_sources,
    run_online_fallback,
)
from upgradelens.docs.rag_miss import RagMissReason, classify_rag_miss
from upgradelens.domain.doc_evidence import DocEvidence, RetrievalRun
from upgradelens.llm.gateway import ModelMode
from upgradelens.tools.errors import OutOfNetworkError
from upgradelens.tools.fetcher import FetchResult
from upgradelens.tools.trace import ToolTrace


class _FakeFetcher:
    """In-memory stand-in for RestrictedFetcher: url -> (text, content_type)."""

    def __init__(self, mapping: dict[str, tuple[str, str]]):
        self.mapping = mapping
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url in self.mapping:
            text, ctype = self.mapping[url]
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                content=text.encode("utf-8"),
                content_type=ctype,
                etag=None,
            )
        raise OutOfNetworkError(f"no fake mapping for {url!r}")


PYPI_JSON = json.dumps(
    {
        "info": {
            "project_urls": {
                "Documentation": "https://requests.readthedocs.io/",
                "Source": "https://github.com/psf/requests",
                "Blog": "https://someblog.example.com/changelog",
            },
            "docs_url": "https://requests.readthedocs.io/",
            "home_page": "https://requests.readthedocs.io/",
        }
    }
)

DOCS_MD = (
    "# Requests\n"
    "## Migrating to 2.0\n"
    "In 2.0 the Response.json method changed. This is a breaking change in 2.x.\n"
    "Upgrade notes: use response.json() instead of response.json.\n"
)


class TestClassifyRagMiss(TestCase):
    def test_no_db(self):
        self.assertEqual(
            classify_rag_miss(has_db=False, has_sources=False, has_covering_source=False, runs=[]),
            RagMissReason.NO_DB,
        )

    def test_no_package(self):
        self.assertEqual(
            classify_rag_miss(has_db=True, has_sources=False, has_covering_source=False, runs=[]),
            RagMissReason.NO_PACKAGE,
        )

    def test_ok_when_served(self):
        ev = DocEvidence(
            evidence_id="e1",
            source_id="s",
            url="u",
            title="t",
            chunk_title="t",
            snapshot_hash="h",
            snippet="snip",
            score=1.0,
            matched_query="q",
            package_name="requests",
            source_version_spec="2.25",
            target_version_spec="2.31",
        )
        run = RetrievalRun(run_id="r", source_id="s", query="q", top_doc_evidence=[ev])
        self.assertEqual(
            classify_rag_miss(has_db=True, has_sources=True, has_covering_source=True, runs=[run]),
            RagMissReason.OK,
        )

    def test_query_no_hit(self):
        self.assertEqual(
            classify_rag_miss(has_db=True, has_sources=True, has_covering_source=True, runs=[]),
            RagMissReason.QUERY_NO_HIT,
        )


class TestDiscoverAndFallback(TestCase):
    def _fetcher(self) -> _FakeFetcher:
        return _FakeFetcher(
            {
                "https://pypi.org/pypi/requests/json": (PYPI_JSON, "application/json"),
                "https://requests.readthedocs.io/": (DOCS_MD, "text/html"),
                "https://someblog.example.com/changelog": (DOCS_MD, "text/html"),
            }
        )

    def test_discover_sorts_official_first(self):
        sources = discover_sources("requests", fetcher=self._fetcher())
        self.assertTrue(sources)
        self.assertEqual(sources[0].trust_hint, "official")  # readthedocs.io
        self.assertIn("https://someblog.example.com/changelog", [s.url for s in sources])

    def test_trust_hint_allowlist(self):
        official = DiscoveredSource(url="https://requests.readthedocs.io/", title="docs").trust_hint
        community = DiscoveredSource(url="https://someblog.example.com/x", title="docs").trust_hint
        self.assertEqual(official, "official")
        self.assertEqual(community, "community")

    def test_run_online_fallback_produces_evidence(self):
        trace = ToolTrace()
        result = run_online_fallback(
            "requests",
            "2.25",
            "2.31",
            "upgrade requests to 2.x",
            ["Response.json"],
            fetcher=self._fetcher(),
            network="online_fallback",
            trace=trace,
        )
        self.assertIsInstance(result, OnlineFallbackResult)
        self.assertTrue(result.evidence)
        self.assertTrue(result.runs)
        for ev in result.evidence:
            self.assertIsInstance(ev, DocEvidence)
            self.assertEqual(ev.provenance, "online_fallback")
            self.assertIn(ev.trust_level, ("official", "community"))
        tools = {e.tool for e in trace.events}
        expected_events = (
            "rag_miss",
            "discover",
            "fetch",
            "temporary_retrieve",
            "online_supplement",
        )
        for expected in expected_events:
            self.assertIn(expected, tools)

    def test_offline_mode_skips_network(self):
        fetcher = self._fetcher()
        result = run_online_fallback(
            "requests",
            "2.25",
            "2.31",
            "upgrade requests",
            [],
            fetcher=fetcher,
            network="offline",
        )
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.runs, [])
        self.assertEqual(fetcher.calls, [])  # never fetched

    def test_graceful_failure_on_fetch_error(self):
        failing = _FakeFetcher({})  # every fetch raises OutOfNetworkError
        trace = ToolTrace()
        result = run_online_fallback(
            "requests",
            "2.25",
            "2.31",
            "upgrade requests",
            [],
            fetcher=failing,
            network="online_fallback",
            trace=trace,
        )
        # No exception escapes; result is empty and explained via trace.
        self.assertEqual(result.runs, [])
        self.assertIn("rag_miss", {e.tool for e in trace.events})


class TestRegistryGate(TestCase):
    def _ctx(self, mode: ModelMode) -> object:
        class _Ctx:
            gateway = SimpleNamespace(mode=mode)
            embedding = None
            trace = ToolTrace()

            def session(self, path):
                return object()

        return _Ctx()

    def _args(self):
        from upgradelens.tools.registry import RetrieveForPackageInput

        return RetrieveForPackageInput(
            package="requests",
            source_version="2.25",
            target_version="2.31",
            db=":memory:",
        )

    def test_live_triggers_fallback(self):
        from upgradelens.tools import registry

        captured = {}
        with mock.patch.object(registry, "iter_sources_for_package", return_value=[]), \
                mock.patch.object(registry, "_retrieve_for_package", return_value=[]), \
                mock.patch.object(
                    registry,
                    "run_online_fallback",
                    side_effect=lambda *a, **k: captured.update(k)
                    or OnlineFallbackResult(
                        runs=[
                            RetrievalRun(
                                run_id="online-x",
                                source_id="online:requests",
                                query="q",
                            )
                        ]
                    ),
                ):
            out = registry._handle_retrieve_for_package(self._args(), self._ctx(ModelMode.LIVE))
        self.assertTrue(captured, "run_online_fallback should be called in live mode")
        # The fallback run is merged into the tool output.
        self.assertEqual(len(out), 1)

    def test_fake_never_calls_network(self):
        from upgradelens.tools import registry

        with mock.patch.object(registry, "iter_sources_for_package", return_value=[]), \
                mock.patch.object(registry, "_retrieve_for_package", return_value=[]), \
                mock.patch.object(registry, "run_online_fallback") as fb:
            registry._handle_retrieve_for_package(self._args(), self._ctx(ModelMode.FAKE))
        fb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
