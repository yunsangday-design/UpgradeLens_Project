"""Source symbol + import extraction (plan stage S3).

Deterministic, AST-based extraction for Python. Other languages fall back to no symbols
(the impact analyzer simply has less to work with) -- the rest of the pipeline never
depends on symbols being present.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from upgradelens.repository.models import CodeSymbol

__all__ = ["extract_symbols", "module_imports", "module_name_for_path"]


_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def extract_symbols(path: str | Path, content: str | None = None) -> list[CodeSymbol]:
    """Return the named definitions found in a Python source file."""
    from upgradelens.repository.models import CodeSymbol

    if content is None:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    symbols: list[CodeSymbol] = []
    for node in ast.walk(tree):
        if isinstance(node, _DEF_NODES):
            symbols.append(
                CodeSymbol(
                    name=node.name,
                    kind=type(node).__name__,
                    path=str(path),
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                )
            )
    return symbols


def module_imports(content: str) -> list[str]:
    """Return the dotted module names a Python source file imports."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def module_name_for_path(root: str | Path, path: str | Path) -> str | None:
    """Best-effort dotted module name for a repo-relative path (Python only)."""
    p = Path(path)
    if p.suffix != ".py":
        return None
    root_path = Path(root)
    try:
        rel = p.relative_to(root_path)
    except ValueError:
        rel = p
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None
