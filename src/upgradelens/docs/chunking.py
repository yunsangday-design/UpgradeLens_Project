"""Heading-aware chunking for documentation (stage 4).

The chunker builds a heading tree from a cleaned Markdown document and emits one
:class:`~upgradelens.domain.doc_evidence.DocChunk` per node that carries body
text. Each chunk remembers the full ``heading_path`` from the document root down
to it, and code fences are preserved verbatim (they live inside the body text).
"""

from __future__ import annotations

import re
from typing import TypedDict

from upgradelens.domain.doc_evidence import DocChunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#*)?\s*$")


class _Node(TypedDict):
    level: int
    title: str
    body: list[str]
    children: list[_Node]


def _parse_tree(md: str) -> list[_Node]:
    """Return a list of heading nodes with ``level``/``title``/``body``/``children``."""
    lines = md.splitlines()
    root: list[_Node] = []
    stack: list[_Node] = []
    for line in lines:
        match = _HEADING_RE.match(line)
        if not match:
            if stack:
                stack[-1]["body"].append(line)
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        node: _Node = {"level": level, "title": title, "body": [], "children": []}
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            root.append(node)
        stack.append(node)
    return root


def _flatten(nodes: list[_Node], path_prefix: list[str], source_id: str) -> list[DocChunk]:
    chunks: list[DocChunk] = []
    for node in nodes:
        path = path_prefix + [node["title"]]
        body = "\n".join(node["body"]).strip()
        if body:
            chunks.append(
                DocChunk(
                    source_id=source_id,
                    title=node["title"],
                    heading_path=path,
                    content=body,
                ).with_hash()
            )
        chunks.extend(_flatten(node["children"], path, source_id))
    return chunks


def chunk_markdown(md: str, source_id: str) -> list[DocChunk]:
    """Split a cleaned Markdown document into heading-aware chunks."""
    chunks = _flatten(_parse_tree(md), [], source_id)
    if chunks:
        return chunks
    # Document without any heading: emit a single whole-document chunk.
    text = "\n".join(md.splitlines()).strip()
    if not text:
        return []
    return [
        DocChunk(
            source_id=source_id,
            title="document",
            heading_path=["document"],
            content=text,
        ).with_hash()
    ]
