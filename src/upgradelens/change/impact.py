"""Deterministic change-impact analysis (plan stage S3).

Given a parsed :class:`ChangeSet` and a repo root, we map changed hunks to the
symbols they touch (direct), then walk one hop along Python ``import`` edges to find
the symbols in files that depend on a changed module (impacted). Pure static analysis
-- no model, no network -- which is exactly what makes S4 (PR review) and S6 (issue
repair) safe to drive from a diff.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.change.models import ChangeLabel, ChangeSet
from upgradelens.change.symbols import extract_symbols, module_imports, module_name_for_path
from upgradelens.repository.models import CodeSymbol

__all__ = ["SymbolImpact", "ChangeImpact", "analyze_impact"]


class SymbolImpact(BaseModel):
    """A symbol affected by the change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: CodeSymbol
    label: ChangeLabel
    is_direct: bool


class ChangeImpact(BaseModel):
    """Result of :func:`analyze_impact`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direct: list[CodeSymbol] = Field(default_factory=list)
    impacted: list[CodeSymbol] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    summary: str = ""


def _index_repo(
    root: Path,
) -> tuple[dict[str, list[CodeSymbol]], dict[str, str], dict[str, set[str]]]:
    """Return (symbols_by_path, module_of_path, imports_by_module)."""
    symbols_by_path: dict[str, list[CodeSymbol]] = {}
    module_of_path: dict[str, str] = {}
    imports_by_module: dict[str, set[str]] = {}
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for path in root.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        symbols_by_path[rel] = extract_symbols(rel, text)
        mod = module_name_for_path(root, rel)
        if mod:
            module_of_path[rel] = mod
            imports_by_module[mod] = set(module_imports(text))
    return symbols_by_path, module_of_path, imports_by_module


def _symbol_overlaps_hunks(symbol: CodeSymbol, changeset: ChangeSet, path: str) -> bool:
    for fc in changeset.files:
        if fc.path != path:
            continue
        for hunk in fc.hunks:
            start = hunk.new_start
            end = hunk.new_start + hunk.new_count
            lo = symbol.lineno
            hi = symbol.end_lineno if symbol.end_lineno is not None else symbol.lineno
            if lo <= end and hi >= start:
                return True
    return False


def analyze_impact(changeset: ChangeSet, root: str | Path) -> ChangeImpact:
    """Compute direct + one-hop impacted symbols for ``changeset`` under ``root``."""
    root_path = Path(root)
    symbols_by_path, module_of_path, imports_by_module = _index_repo(root_path)

    direct: list[CodeSymbol] = []
    changed_modules: set[str] = set()
    labels: dict[str, str] = {}

    for fc in changeset.files:
        labels[fc.path] = fc.label.value
        mod = module_name_for_path(root_path, fc.path)
        if mod:
            changed_modules.add(mod)
        file_symbols = symbols_by_path.get(fc.path, [])
        if fc.label is ChangeLabel.ADDED:
            direct.extend(file_symbols)
        elif fc.label is ChangeLabel.MODIFIED:
            for sym in file_symbols:
                if _symbol_overlaps_hunks(sym, changeset, fc.path):
                    direct.append(sym)
        elif fc.label is ChangeLabel.DELETED:
            # Symbol definitions are gone; record last-known symbols as removed.
            direct.extend(file_symbols)

    # One-hop impacted: files importing a changed module.
    impacted: list[CodeSymbol] = []
    direct_ids = {(s.path, s.name, s.lineno) for s in direct}
    for mod, imported in imports_by_module.items():
        if imported & changed_modules:
            # find the path(s) for this module and add their symbols
            for path, m in module_of_path.items():
                if m == mod:
                    for sym in symbols_by_path.get(path, []):
                        if (sym.path, sym.name, sym.lineno) not in direct_ids:
                            impacted.append(sym)

    summary = (
        f"{len(direct)} direct, {len(impacted)} impacted across "
        f"{changeset.stat.files_changed} changed file(s)"
    )
    return ChangeImpact(
        direct=direct, impacted=impacted, labels=labels, summary=summary
    )
