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
            "discover-l1",
            "fetch",
            "temporary_retrieve-l1",
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
        with (
            mock.patch.object(registry, "iter_sources_for_package", return_value=[]),
            mock.patch.object(registry, "_retrieve_for_package", return_value=[]),
            mock.patch.object(
                registry,
                "run_online_fallback",
                side_effect=lambda *a, **k: (
                    captured.update(k)
                    or OnlineFallbackResult(
                        runs=[
                            RetrievalRun(
                                run_id="online-x",
                                source_id="online:requests",
                                query="q",
                            )
                        ]
                    )
                ),
            ),
        ):
            out = registry._handle_retrieve_for_package(self._args(), self._ctx(ModelMode.LIVE))
        self.assertTrue(captured, "run_online_fallback should be called in live mode")
        # The fallback run is merged into the tool output.
        self.assertEqual(len(out), 1)

    def test_fake_never_calls_network(self):
        from upgradelens.tools import registry

        with (
            mock.patch.object(registry, "iter_sources_for_package", return_value=[]),
            mock.patch.object(registry, "_retrieve_for_package", return_value=[]),
            mock.patch.object(registry, "run_online_fallback") as fb,
        ):
            registry._handle_retrieve_for_package(self._args(), self._ctx(ModelMode.FAKE))
        fb.assert_not_called()


# -- WebSearchProvider & two-stage fallback tests --------------------------------

# A fake DuckDuckGo HTML result page containing two result links.
DDG_HTML = """<!DOCTYPE html>
<html>
<body>
<div class="results">
  <div class="result">
    <a class="result__a" href="https://someblog.example.com/migration-v2-to-v3">
      Migrating from v2 to v3 — complete guide
    </a>
    <a class="result__snippet" href="https://someblog.example.com/migration-v2-to-v3">
      This guide covers all breaking changes when upgrading from v2 to v3...
    </a>
  </div>
  <div class="result">
    <a class="result__a" href="https://github.com/acme/parcel/releases/tag/v3.0.0">
      Release v3.0.0 - breaking changes
    </a>
  </div>
  <div class="result">
    <!-- Noise: DuckDuckGo internal -->
    <a class="result__a" href="https://duckduckgo.com/tracker/foo">Tracker</a>
  </div>
  <div class="result">
    <!-- Ad: doubleclick -->
    <a class="result__a" href="https://ad.doubleclick.net/foo">Ad</a>
  </div>
  <div class="result">
    <!-- Google search link (noise) -->
    <a class="result__a" href="https://www.google.com/search?q=parcel">Google</a>
  </div>
</div>
</body>
</html>
"""

# DuckDuckGo with uddg= redirect wrapping.
DDG_HTML_UDDG = """<!DOCTYPE html>
<html>
<body>
<div class="results">
  <div class="result">
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fupgrade">docs</a>
  </div>
</div>
</body>
</html>
"""

DOCS_V3 = (
    "# Parcel v3 Migration\n"
    "The `send_message` API was removed in v3. Use `dispatch` instead.\n"
    "Breaking change: `ParcelConfig.timeout` renamed to `ParcelConfig.request_timeout`.\n"
)


NO_DOCS_PYPI_JSON = json.dumps({"info": {"project_urls": None, "docs_url": None, "home_page": None}})


class TestWebSearchProvider(TestCase):
    """Tests for WebSearchProvider that searches DuckDuckGo for migration docs."""

    @staticmethod
    def _provider():
        from upgradelens.docs.online_fallback import WebSearchProvider
        return WebSearchProvider()

    def test_discovers_urls_from_ddg_html(self):
        fetcher = _FakeFetcher(
            {
                "https://html.duckduckgo.com/html/?q=parcel%20upgrade%20migration%20guide%20breaking%20changes": (
                    DDG_HTML,
                    "text/html",
                ),
                "https://html.duckduckgo.com/html/?q=parcel%20changelog%20release%20notes": (
                    "",  # empty — no second-search results
                    "text/html",
                ),
            }
        )
        sources = self._provider().discover("parcel", fetcher=fetcher)
        urls = [s.url for s in sources]
        # Two productive links from the first search.
        self.assertIn("https://someblog.example.com/migration-v2-to-v3", urls)
        self.assertIn("https://github.com/acme/parcel/releases/tag/v3.0.0", urls)
        # Noise filtered.
        self.assertNotIn("https://duckduckgo.com/tracker/foo", urls)
        self.assertNotIn("https://ad.doubleclick.net/foo", urls)
        self.assertNotIn("https://www.google.com/search?q=parcel", urls)

    def test_unwraps_ddg_redirect(self):
        fetcher = _FakeFetcher(
            {
                "https://html.duckduckgo.com/html/?q=parcel%20upgrade%20migration%20guide%20breaking%20changes": (
                    DDG_HTML_UDDG,
                    "text/html",
                ),
                "https://html.duckduckgo.com/html/?q=parcel%20changelog%20release%20notes": (
                    "",
                    "text/html",
                ),
            }
        )
        sources = self._provider().discover("parcel", fetcher=fetcher)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].url, "https://docs.example.com/upgrade")

    def test_all_kinds_are_web_search(self):
        fetcher = _FakeFetcher(
            {
                "https://html.duckduckgo.com/html/?q=parcel%20upgrade%20migration%20guide%20breaking%20changes": (
                    DDG_HTML,
                    "text/html",
                ),
                "https://html.duckduckgo.com/html/?q=parcel%20changelog%20release%20notes": (
                    "",
                    "text/html",
                ),
            }
        )
        sources = self._provider().discover("parcel", fetcher=fetcher)
        for s in sources:
            self.assertEqual(s.kind, "web_search")

    def test_empty_on_fetch_error(self):
        fetcher = _FakeFetcher({})  # every fetch raises
        sources = self._provider().discover("parcel", fetcher=fetcher)
        self.assertEqual(sources, [])

    def test_deduplicates_across_queries(self):
        same_html = DDG_HTML  # both queries return identical results
        fetcher = _FakeFetcher(
            {
                "https://html.duckduckgo.com/html/?q=parcel%20upgrade%20migration%20guide%20breaking%20changes": (
                    same_html,
                    "text/html",
                ),
                "https://html.duckduckgo.com/html/?q=parcel%20changelog%20release%20notes": (
                    same_html,
                    "text/html",
                ),
            }
        )
        sources = self._provider().discover("parcel", fetcher=fetcher)
        urls = [s.url for s in sources]
        self.assertEqual(len(urls), len(set(urls)), "URLs must be deduplicated")


class TestTwoStageFallback(TestCase):
    """Verify that run_online_fallback tries PyPI first, then web search
    only when the first stage yields zero evidence."""

    def _fetcher_pypi_works(self):
        # PyPI returns good json + docs URL with useful content → Stage 1 succeeds
        return _FakeFetcher(
            {
                "https://pypi.org/pypi/requests/json": (PYPI_JSON, "application/json"),
                "https://requests.readthedocs.io/": (DOCS_MD, "text/html"),
                "https://someblog.example.com/changelog": (DOCS_MD, "text/html"),
            }
        )

    def _fetcher_pypi_no_evidence(self):
        """PyPI JSON has ``project_urls`` → a URL is discovered and fetched,
        but its content doesn't contain any term-match evidence for the query."""
        return _FakeFetcher(
            {
                "https://pypi.org/pypi/requests/json": (
                    json.dumps(
                        {
                            "info": {
                                "project_urls": {"Homepage": "https://someblog.example.com/about"},
                                "docs_url": None,
                                "home_page": None,
                            }
                        }
                    ),
                    "application/json",
                ),
                # The discovered URL pages contain NO upgrade/migration keywords.
                "https://someblog.example.com/about": (
                    "<html><body>We are Acme Inc. Our product is great.</body></html>",
                    "text/html",
                ),
            }
        )

    def _fetcher_pypi_has_no_urls(self):
        """PyPI JSON is valid but has empty project_urls / docs_url / home_page."""
        return _FakeFetcher(
            {
                "https://pypi.org/pypi/nobody-knows/json": (NO_DOCS_PYPI_JSON, "application/json"),
            }
        )

    def _fetcher_both_stages(self):
        """PyPI has NO docs → web search (DDG) discovers useful blog post."""
        return _FakeFetcher(
            {
                # Stage 1: PyPI — no project_urls / docs_url / home_page
                "https://pypi.org/pypi/nobody-knows/json": (NO_DOCS_PYPI_JSON, "application/json"),
                # Stage 2: DuckDuckGo search
                "https://html.duckduckgo.com/html/?q=nobody-knows%20upgrade%20migration%20guide%20breaking%20changes": (
                    DDG_HTML,
                    "text/html",
                ),
                "https://html.duckduckgo.com/html/?q=nobody-knows%20changelog%20release%20notes": (
                    "",
                    "text/html",
                ),
                # Content pages discovered by DDG
                "https://someblog.example.com/migration-v2-to-v3": (DOCS_V3, "text/html"),
                "https://github.com/acme/parcel/releases/tag/v3.0.0": (DOCS_V3, "text/html"),
            }
        )

    def test_stage1_succeeds_stage2_skipped(self):
        """When PyPI produces evidence, web search must NOT be triggered."""
        fetcher = self._fetcher_pypi_works()
        trace = ToolTrace()
        result = run_online_fallback(
            "requests",
            "2.25",
            "2.31",
            "upgrade requests",
            ["Response.json"],
            fetcher=fetcher,
            network="online_fallback",
            trace=trace,
        )
        self.assertTrue(result.evidence, "Stage 1 should produce evidence")
        tools = {e.tool for e in trace.events}
        self.assertIn("discover-l1", tools)
        # Stage 2 must NOT be reached
        self.assertNotIn("online_fallback_stage2", tools)
        self.assertNotIn("discover-l2", tools)

    def test_stage1_no_sources_falls_back_to_stage2(self):
        """PyPI has no URLs → web search discovers and retrieves evidence."""
        fetcher = self._fetcher_both_stages()
        trace = ToolTrace()
        result = run_online_fallback(
            "nobody-knows",
            "2.0",
            "3.0",
            "upgrade nobody-knows to v3",
            ["send_message", "ParcelConfig"],
            fetcher=fetcher,
            network="online_fallback",
            trace=trace,
        )
        self.assertTrue(
            result.evidence,
            f"Stage 2 must produce evidence; got status={result.status!r}",
        )
        tools = {e.tool for e in trace.events}
        self.assertIn("discover-l1", tools)
        self.assertIn("online_fallback_stage2", tools)
        self.assertIn("discover-l2", tools)

    def test_stage1_pypi_has_urls_but_no_evidence_falls_back(self):
        """PyPI discovers a URL, fetches it, but term-match returns empty
        → Stage 2 (web search) must be attempted."""
        # Even though Stage 1 "found" a URL, the content has no upgrade terms,
        # so temporary_retrieve returns empty → triggers Stage 2.
        # We wire the fetcher so Stage 2's DDG search points to the same no-evidence
        # URL — both stages fail gracefully, producing "failed" status but no
        # exception.
        pypi_fetcher = self._fetcher_pypi_no_evidence()
        trace = ToolTrace()
        result = run_online_fallback(
            "requests",
            "2.25",
            "2.31",
            "upgrade requests to latest",
            ["Response.json"],
            fetcher=pypi_fetcher,
            network="online_fallback",
            trace=trace,
        )
        tools = {e.tool for e in trace.events}
        # Stage 1 ran, got empty evidence → Stage 2 invoked
        self.assertIn("online_fallback_stage2", tools)
        self.assertIn("discover-l2", tools)
        # Both stages failed to produce evidence → result is empty
        self.assertEqual(result.runs, [])
        self.assertIn(
            result.status,
            ("failed", "partial"),
            f"Expected failed/partial, got {result.status!r}",
        )

    def test_stage1_no_sources_but_stage2_also_fails(self):
        """PyPI has no URLs, and web search also fails → graceful empty."""
        # Stage 1: PyPI empty → falls back
        # Stage 2: DDG query raises (no mapping) → empty
        empty_fetcher = _FakeFetcher({})  # everything fails
        trace = ToolTrace()
        result = run_online_fallback(
            "ghost",
            "1.0",
            "2.0",
            "upgrade ghost",
            [],
            fetcher=empty_fetcher,
            network="online_fallback",
            trace=trace,
        )
        self.assertEqual(result.runs, [])
        self.assertEqual(result.status, "failed")
        # No exception — just an empty result.


if __name__ == "__main__":
    unittest.main()
