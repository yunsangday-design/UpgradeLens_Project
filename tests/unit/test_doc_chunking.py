"""Tests for heading-aware chunking (stage 4)."""

from __future__ import annotations

from upgradelens.docs.chunking import chunk_markdown


def test_heading_path_reflects_hierarchy() -> None:
    md = "# Root\n\nintro\n\n## Child A\n\nbody A\n\n## Child B\n\nbody B\n"
    chunks = chunk_markdown(md, "src")
    titles = [c.title for c in chunks]
    assert "Root" in titles
    assert "Child A" in titles
    assert "Child B" in titles
    child_a = next(c for c in chunks if c.title == "Child A")
    assert child_a.heading_path == ["Root", "Child A"]
    child_b = next(c for c in chunks if c.title == "Child B")
    assert child_b.heading_path == ["Root", "Child B"]


def test_code_block_preserved() -> None:
    md = "# R\n\n## S\n\ntext\n\n```\ncode_here()\n```\n"
    chunks = chunk_markdown(md, "src")
    assert any("code_here()" in c.content for c in chunks)


def test_content_hash_populated() -> None:
    chunks = chunk_markdown("# A\n\nbody\n", "src")
    assert chunks
    assert chunks[0].content_hash
    assert len(chunks[0].content_hash) == 64


def test_document_without_headings_single_chunk() -> None:
    md = "just some prose without any heading\n"
    chunks = chunk_markdown(md, "src")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["document"]
