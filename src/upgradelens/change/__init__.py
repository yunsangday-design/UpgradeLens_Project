"""Deterministic change-set + impact analysis (plan stage S3)."""

from __future__ import annotations

from upgradelens.change.diff import is_safe_path, language_for_path, parse_unified_diff
from upgradelens.change.git import collect_git_diff, collect_workspace_diff
from upgradelens.change.impact import ChangeImpact, SymbolImpact, analyze_impact
from upgradelens.change.models import (
    ChangeHunk,
    ChangeLabel,
    ChangeSet,
    DiffStat,
    FileChange,
)
from upgradelens.change.symbols import (
    extract_symbols,
    module_imports,
    module_name_for_path,
)

__all__ = [
    "ChangeSet",
    "ChangeLabel",
    "FileChange",
    "ChangeHunk",
    "DiffStat",
    "parse_unified_diff",
    "is_safe_path",
    "language_for_path",
    "collect_git_diff",
    "collect_workspace_diff",
    "extract_symbols",
    "module_imports",
    "module_name_for_path",
    "analyze_impact",
    "ChangeImpact",
    "SymbolImpact",
]
