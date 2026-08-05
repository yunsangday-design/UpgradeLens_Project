"""HTML/Markdown cleaning for the documentation pipeline (stage 4).

The cleaner extracts the main readable text from a documentation snapshot,
strips navigation/chrome (``script``, ``style``, ``nav``, ``header`` …), keeps
``<pre><code>`` blocks verbatim (so API signatures survive), and emits a simple
Markdown document whose ``#`` headings drive the heading-aware chunker.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_SKIP_TAGS = {
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "aside",
    "noscript",
    "svg",
    "head",
}
# Void elements carry no text content; treating them as skip tags would leave
# ``_skip_depth`` permanently raised because they have no closing tag.
_VOID_TAGS = {
    "meta",
    "link",
    "img",
    "input",
    "br",
    "hr",
    "area",
    "base",
    "col",
    "embed",
    "source",
    "track",
    "wbr",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "tr",
    "section",
    "article",
    "blockquote",
    "ul",
    "ol",
    "table",
    "dt",
    "dd",
}
_FRONTMATTER = re.compile(r"^\s*---\n.*?\n---\n", re.DOTALL)


class _HtmlToMarkdown(HTMLParser):
    """Minimal HTML → Markdown converter focused on documentation bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_code_block = 0
        self._code_buf: list[str] = []
        self._out: list[str] = []

    # -- structural tags ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "pre":
            self._in_code_block += 1
            self._code_buf = []
            return
        if tag == "code":
            if self._in_code_block == 0:
                self._out.append("`")
            return
        if tag in _HEADING_TAGS:
            level = int(tag[1])
            self._out.append("\n\n" + "#" * level + " ")
            return
        if tag in ("br", "hr"):
            self._out.append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "pre":
            code = "".join(self._code_buf).rstrip("\n")
            self._in_code_block -= 1
            self._out.append("\n\n```\n" + code + "\n```\n\n")
            return
        if tag == "code":
            if self._in_code_block == 0:
                self._out.append("`")
            return
        if tag in _HEADING_TAGS:
            self._out.append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_code_block > 0:
            self._code_buf.append(data)
            return
        self._out.append(data)


def clean_html(raw: str) -> str:
    """Convert an HTML snapshot to a clean Markdown document."""
    parser = _HtmlToMarkdown()
    parser.feed(raw)
    text = "".join(parser._out)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip() + "\n"


def clean_markdown(raw: str) -> str:
    """Normalise a Markdown snapshot (strip frontmatter, fix line endings)."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _FRONTMATTER.sub("", text, count=1)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean_document(raw: str, strategy: str) -> str:
    """Clean ``raw`` according to ``strategy`` (``html``/``markdown``/``static``)."""
    if strategy == "html":
        return clean_html(raw)
    return clean_markdown(raw)
