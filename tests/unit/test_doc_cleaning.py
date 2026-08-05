"""Tests for HTML/Markdown cleaning (stage 4)."""

from __future__ import annotations

from upgradelens.docs.cleaning import clean_html, clean_markdown


def test_clean_html_strips_navigation_and_scripts() -> None:
    raw = (
        "<html><head><script>track()</script><style>x{}</style></head>"
        "<body><nav>menu item</nav><header>banner</header>"
        "<h1>Title</h1><p>Body text here</p>"
        "<pre><code>def abc(): pass</code></pre>"
        "<aside>spam</aside></body></html>"
    )
    out = clean_html(raw)
    assert "menu item" not in out
    assert "banner" not in out
    assert "spam" not in out
    assert "track()" not in out
    assert "Title" in out
    assert "Body text here" in out
    assert "def abc(): pass" in out
    assert "```" in out


def test_clean_html_preserves_heading_hierarchy() -> None:
    raw = "<body><h1>Root</h1><h2>Child</h2><h3>Leaf</h3></body>"
    out = clean_html(raw)
    assert "# Root" in out
    assert "## Child" in out
    assert "### Leaf" in out


def test_clean_markdown_strips_frontmatter() -> None:
    raw = "---\ntitle: x\n---\n# Heading\n\nbody\n"
    out = clean_markdown(raw)
    assert "title: x" not in out
    assert "# Heading" in out
    assert "body" in out
