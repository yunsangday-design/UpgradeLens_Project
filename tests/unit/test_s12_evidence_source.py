"""Tests for step12 increment F: evidence source label normalisation.

F.1: temporary_retrieve uses source trust_hint (not hardcoded "community").
F.2: presentation layer exposes provenance + from_network + trust label suffix.
F.3: DOC_SOURCE_UNTRUSTED removed from IssueCode enum & remediation map.
"""

from __future__ import annotations

import pytest

from upgradelens.docs.online_fallback import (
    DiscoveredSource,
    temporary_retrieve,
)
from upgradelens.domain.doc_evidence import DocChunk
from upgradelens.models.impact import EvidenceItem
from upgradelens.presentation.i18n import (
    issue_code_label,
    trust_label_with_provenance,
)
from upgradelens.presentation.projector import _doc_view, _rag_view
from upgradelens.verify.models import REMEDIATION_FOR_ISSUE, IssueCode

# ---------------------------------------------------------------------------
# F.1: temporary_retrieve trust_level from source_trust_map
# ---------------------------------------------------------------------------


def _make_chunk(source_id: str, content: str) -> DocChunk:
    return DocChunk(
        source_id=source_id,
        title="test",
        heading_path=["test"],
        content=content,
    ).with_hash()


class TestTemporaryRetrieveTrust:
    """temporary_retrieve should honour source_trust_map."""

    def test_official_host_gets_official_trust(self):
        """Chunk from readthedocs URL → trust_level == official."""
        url = "https://sqlalchemy.readthedocs.io/en/latest/changelog.html"
        chunk = _make_chunk(url, "breaking change migration upgrade v2")
        evidence = temporary_retrieve(
            "sqlalchemy",
            "1.4",
            "2.0",
            "upgrade migration",
            ["DeclarativeBase"],
            [chunk],
            source_trust_map={url: "official"},
        )
        assert len(evidence) == 1
        assert evidence[0].trust_level == "official"
        assert evidence[0].provenance == "online_fallback"

    def test_community_host_gets_community_trust(self):
        """Chunk from random blog → trust_level == community."""
        url = "https://some-blog.dev/sqlalchemy-upgrade.html"
        chunk = _make_chunk(url, "breaking change migration upgrade v2")
        evidence = temporary_retrieve(
            "sqlalchemy",
            "1.4",
            "2.0",
            "upgrade migration",
            [],
            [chunk],
            source_trust_map={url: "community"},
        )
        assert len(evidence) == 1
        assert evidence[0].trust_level == "community"

    def test_missing_from_map_defaults_community(self):
        """When source_trust_map lacks the URL, default to community."""
        url = "https://unknown.io/page"
        chunk = _make_chunk(url, "breaking change migration upgrade v2")
        evidence = temporary_retrieve(
            "pkg", "1.0", "2.0", "upgrade", [], [chunk], source_trust_map={}
        )
        assert evidence[0].trust_level == "community"

    def test_no_map_defaults_community(self):
        """When source_trust_map is None, default to community."""
        url = "https://unknown.io/page"
        chunk = _make_chunk(url, "breaking change migration upgrade v2")
        evidence = temporary_retrieve("pkg", "1.0", "2.0", "upgrade", [], [chunk])
        assert evidence[0].trust_level == "community"

    def test_mixed_sources_get_correct_trust(self):
        """Chunks from different sources get different trust levels."""
        official_url = "https://docs.python.org/3/whatsnew.html"
        blog_url = "https://blog.example.com/notes.html"
        chunks = [
            _make_chunk(official_url, "breaking change migration upgrade v3"),
            _make_chunk(blog_url, "breaking change migration upgrade v3"),
        ]
        evidence = temporary_retrieve(
            "pkg",
            "2.0",
            "3.0",
            "upgrade migration",
            [],
            chunks,
            source_trust_map={official_url: "official", blog_url: "community"},
        )
        trust_by_url = {e.url: e.trust_level for e in evidence}
        assert trust_by_url[official_url] == "official"
        assert trust_by_url[blog_url] == "community"


# ---------------------------------------------------------------------------
# F.1: DiscoveredSource auto-detects official hosts
# ---------------------------------------------------------------------------


class TestDiscoveredSourceTrustHint:
    """DiscoveredSource.__post_init__ should detect official hosts."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://sqlalchemy.readthedocs.io/latest/", "official"),
            ("https://docs.python.org/3/whatsnew.html", "official"),
            ("https://pypi.org/project/sqlalchemy/", "official"),
            ("https://github.com/sqlalchemy/sqlalchemy", "official"),
            ("https://random-blog.dev/post", "community"),
        ],
    )
    def test_trust_hint(self, url: str, expected: str):
        src = DiscoveredSource(url=url, title="test")
        assert src.trust_hint == expected


# ---------------------------------------------------------------------------
# F.2: presentation layer provenance + trust_label suffix
# ---------------------------------------------------------------------------


class TestTrustLabelWithProvenance:
    """trust_label_with_provenance appends network marker."""

    def test_official_local(self):
        assert trust_label_with_provenance("official", "local_corpus") == "官方"

    def test_official_network_zh(self):
        assert trust_label_with_provenance("official", "online_fallback") == "官方（网络）"

    def test_community_network_zh(self):
        assert trust_label_with_provenance("community", "online_fallback") == "社区（网络）"

    def test_official_network_en(self):
        label = trust_label_with_provenance("official", "online_fallback", "en")
        assert label == "Official (network)"

    def test_community_local(self):
        assert trust_label_with_provenance("community", "local_corpus") == "社区"


class TestProjectorProvenance:
    """_doc_view and _rag_view populate provenance/from_network."""

    def _item(self, provenance: str, trust: str) -> EvidenceItem:
        return EvidenceItem(
            evidence_id="e1",
            kind="doc_chunk",
            summary="test summary",
            detail="test detail",
            meta={
                "title": "Test",
                "url": "https://example.com",
                "trust_level": trust,
                "provenance": provenance,
                "heading_path": [],
                "snippet": "...",
                "source_version": "1.0",
                "target_version": "2.0",
                "source_id": "src",
                "chunk_title": "chunk",
                "snapshot_hash": "abc",
                "score": 0.9,
                "matched_query": "q",
            },
        )

    def test_doc_view_network(self):
        item = self._item("online_fallback", "official")
        view = _doc_view(item)
        assert view.provenance == "online_fallback"
        assert view.from_network is True
        assert "网络" in view.trust_label

    def test_doc_view_local(self):
        item = self._item("local_corpus", "official")
        view = _doc_view(item)
        assert view.provenance == "local_corpus"
        assert view.from_network is False
        assert "网络" not in view.trust_label

    def test_rag_view_network(self):
        item = self._item("online_fallback", "community")
        view = _rag_view(item)
        assert view.provenance == "online_fallback"
        assert view.from_network is True
        assert "网络" in view.trust_label

    def test_rag_view_local(self):
        item = self._item("local_corpus", "community")
        view = _rag_view(item)
        assert view.from_network is False
        assert "网络" not in view.trust_label


# ---------------------------------------------------------------------------
# F.3: DOC_SOURCE_UNTRUSTED removed
# ---------------------------------------------------------------------------


class TestUntrustedRemoved:
    """DOC_SOURCE_UNTRUSTED should no longer exist."""

    def test_not_in_issue_code_enum(self):
        values = {ic.value for ic in IssueCode}
        assert "doc_source_untrusted" not in values

    def test_not_in_remediation_map(self):
        for code in REMEDIATION_FOR_ISSUE:
            assert code.value != "doc_source_untrusted"

    def test_not_in_i18n(self):
        # Falls back to the raw value when not in dictionary
        label = issue_code_label("doc_source_untrusted")
        assert label == "doc_source_untrusted"  # raw fallback, not a known label
