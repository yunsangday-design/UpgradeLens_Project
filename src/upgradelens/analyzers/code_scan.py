"""Stage 2 Python AST code-evidence scanner (plan section 1652).

Pure static analysis: walk the repository's ``*.py`` files, build module-level
import bindings for the target dependency, then record every place those
bindings are used. No code is imported, installed or executed, and no model is
contacted -- the scanner only reads source text and parses it.

Design rules (see the stage 2 plan):

- file traversal uses a fixed exclude set and never depends on directory order;
- only module-level imports form bindings; function-local imports are ignored;
- a name re-bound by a non-import assignment is flagged ``confidence=low``;
- dynamic imports (``__import__`` / ``importlib.import_module``) are recorded
  separately as uncertain, never counted as normal usages;
- a syntax-error file becomes a structured ``ParseError`` and contributes 0
  usages instead of aborting the whole scan;
- every path is POSIX-relative and no machine-absolute path is emitted.
"""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packaging.utils import canonicalize_name

from upgradelens.domain import (
    CodeEvidenceReport,
    CodeEvidenceSummary,
    CodeUsage,
    DynamicImport,
    ParseError,
    TestProductionLink,
    UsageKind,
)
from upgradelens.platform import read_text_utf8, to_posix_rel_path

__all__ = ["DEFAULT_EXCLUDE_DIRS", "scan_code_evidence"]

# Fixed, OS-independent directory names that never hold first-party source.
_DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "env",
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site-packages",
        ".eggs",
    }
)
_EGG_INFO_SUFFIX = ".egg-info"

TEST_DIR_SEGMENTS = frozenset({"test", "tests", "spec", "specs"})

DEFAULT_EXCLUDE_DIRS: frozenset[str] = _DEFAULT_EXCLUDE_DIRS


@dataclass
class _Binding:
    """A module-level name bound to the target dependency via ``import``."""

    module: str
    symbol: str | None  # imported symbol name; None for a whole-module import
    shadowed: bool = False


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def scan_code_evidence(
    repo_root: Path,
    dependency_name: str,
    *,
    exclude_extra: frozenset[str] = frozenset(),
) -> CodeEvidenceReport:
    """Scan *repo_root* for usages of *dependency_name* and return a report.

    The dependency is matched by its canonical (PEP 503) name, so ``pydantic``
    also matches ``pydantic.v1`` and ``PyDantic`` imports.
    """
    canonical = canonicalize_name(dependency_name)
    root = Path(repo_root).resolve()
    exclude = _DEFAULT_EXCLUDE_DIRS | frozenset(exclude_extra)

    py_files = sorted(_collect_py_files(root, exclude))
    usages: list[CodeUsage] = []
    dynamics: list[DynamicImport] = []
    parse_errors: list[ParseError] = []
    shadowed_total = 0

    for file_path in py_files:
        rel = to_posix_rel_path(root, file_path)
        is_test = _is_test_code(rel)
        try:
            source = read_text_utf8(file_path)
        except (OSError, UnicodeDecodeError) as exc:
            parse_errors.append(
                ParseError(
                    path=rel, message=f"cannot read: {exc.__class__.__name__}", is_test_code=is_test
                )
            )
            continue
        content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            # ast puts the (possibly absolute) filename in str(exc); keep only
            # the message + line so no machine path leaks into the report.
            parse_errors.append(
                ParseError(path=rel, message=f"{exc.msg} (line {exc.lineno})", is_test_code=is_test)
            )
            continue
        file_usages, file_dynamics, shadowed = _scan_tree(
            tree, rel, source, is_test, content_hash, canonical
        )
        usages.extend(file_usages)
        dynamics.extend(file_dynamics)
        shadowed_total += shadowed

    links = _build_test_production_links(root, py_files)
    summary = CodeEvidenceSummary(
        scanned_files=len(py_files),
        usage_count=len(usages),
        by_kind=dict(Counter(u.kind for u in usages)),
        test_code_usages=sum(1 for u in usages if u.is_test_code),
        dynamic_import_count=len(dynamics),
        parse_error_count=len(parse_errors),
        shadowed_binding_count=shadowed_total,
    )
    return CodeEvidenceReport(
        dependency_name=canonical,
        scanned_files=len(py_files),
        usages=usages,
        dynamic_imports=dynamics,
        parse_errors=parse_errors,
        test_production_links=links,
        summary=summary,
    )


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #
def _collect_py_files(root: Path, exclude: frozenset[str]) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in exclude or part.endswith(_EGG_INFO_SUFFIX) for part in rel_parts):
            continue
        found.append(path)
    return found


def _is_test_code(rel: str) -> bool:
    parts = Path(rel).parts
    if any(seg in TEST_DIR_SEGMENTS for seg in parts[:-1]):
        return True
    name = parts[-1]
    if name == "conftest.py":
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


# --------------------------------------------------------------------------- #
# Per-file scanning
# --------------------------------------------------------------------------- #
def _scan_tree(
    tree: ast.Module,
    rel: str,
    source: str,
    is_test: bool,
    content_hash: str,
    canonical: str,
) -> tuple[list[CodeUsage], list[DynamicImport], int]:
    bindings, shadowed_names = _build_bindings(tree, canonical)
    parents = _build_parent_map(tree)
    usages: list[CodeUsage] = []
    dynamics: list[DynamicImport] = []

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            usages.extend(_import_usages(stmt, bindings, source, is_test, content_hash, rel))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dyn = _dynamic_import(node, source, is_test, content_hash, rel, canonical)
            if dyn is not None:
                dynamics.append(dyn)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            usages.extend(_class_base_usages(node, bindings, source, is_test, content_hash, rel))
            usages.extend(
                _class_config_usages(node, parents, bindings, source, is_test, content_hash, rel)
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            usages.extend(_decorator_usages(node, bindings, source, is_test, content_hash, rel))
        elif isinstance(node, ast.Call):
            usages.extend(_call_usages(node, parents, bindings, source, is_test, content_hash, rel))
        elif isinstance(node, ast.Attribute):
            usages.extend(
                _attribute_usages(node, parents, bindings, source, is_test, content_hash, rel)
            )
        elif isinstance(node, ast.Name):
            usages.extend(_name_usages(node, parents, bindings, source, is_test, content_hash, rel))

    shadowed_count = sum(1 for b in bindings.values() if b.shadowed)
    return usages, dynamics, shadowed_count


def _build_parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


# --------------------------------------------------------------------------- #
# Import bindings
# --------------------------------------------------------------------------- #
def _matches_dependency(canonical: str, module: str) -> bool:
    if not module:
        return False
    return canonicalize_name(module.split(".")[0]) == canonical


def _build_bindings(tree: ast.Module, canonical: str) -> tuple[dict[str, _Binding], set[str]]:
    bindings: dict[str, _Binding] = {}
    shadowed_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            shadowed_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                shadowed_names |= _store_names(target)
        elif isinstance(node, ast.AnnAssign):
            shadowed_names |= _store_names(node.target)
        elif isinstance(node, ast.For):
            shadowed_names |= _store_names(node.target)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    shadowed_names |= _store_names(item.optional_vars)

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_dependency(canonical, alias.name):
                    name = alias.asname or alias.name
                    bindings[name] = _Binding(module=alias.name, symbol=None)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:  # relative import: not a third-party dependency
                continue
            if _matches_dependency(canonical, node.module):
                for alias in node.names:
                    if alias.name == "*":  # wildcard: cannot be resolved statically
                        continue
                    name = alias.asname or alias.name
                    bindings[name] = _Binding(module=node.module, symbol=alias.name)

    for name, binding in bindings.items():
        if name in shadowed_names:
            binding.shadowed = True
    return bindings, shadowed_names


def _store_names(node: ast.AST) -> set[str]:
    """Collect simple names bound by a Store context in an assignment target."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
    return names


# --------------------------------------------------------------------------- #
# Binding lookups
# --------------------------------------------------------------------------- #
def _binding_for_node(node: ast.AST, bindings: dict[str, _Binding]) -> _Binding | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            nxt: ast.AST = cur.value
            cur = nxt
        if isinstance(cur, ast.Name):
            return bindings.get(cur.id)
    return None


def _symbol_for(node: ast.AST, bound: _Binding) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return bound.symbol or bound.module


def _alias_of(node: ast.AST, bindings: dict[str, _Binding]) -> str | None:
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        nxt: ast.AST = cur.value
        cur = nxt
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _snippet(source: str, start: int, end: int | None) -> str:
    if end is None:
        end = start
    lines = source.splitlines()
    if start < 1:
        start = 1
    if end > len(lines):
        end = len(lines)
    return "\n".join(lines[start - 1 : end])


def _in_decorator(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur: ast.AST | None = node
    while cur is not None:
        p = parents.get(cur)
        if p is None:
            return False
        if isinstance(p, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            if cur in p.decorator_list:
                return True
        cur = p
    return False


def _in_class_base(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur: ast.AST | None = node
    while cur is not None:
        p = parents.get(cur)
        if p is None:
            return False
        if isinstance(p, ast.ClassDef):
            if cur in p.bases:
                return True
        cur = p
    return False


# --------------------------------------------------------------------------- #
# Usage handlers
# --------------------------------------------------------------------------- #
def _confidence(bound: _Binding | None) -> Literal["high", "low"]:
    return "low" if bound is not None and bound.shadowed else "high"


def _import_usages(
    node: ast.Import | ast.ImportFrom,
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    out: list[CodeUsage] = []
    for alias in node.names:
        key = alias.asname or alias.name
        bound = bindings.get(key)
        if bound is None:
            continue
        out.append(
            CodeUsage(
                path=rel,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                column=node.col_offset,
                kind=UsageKind.IMPORT,
                symbol=bound.symbol or bound.module,
                snippet=_snippet(source, node.lineno, node.end_lineno),
                content_hash=content_hash,
                is_test_code=is_test,
                bound_as=key,
                confidence=_confidence(bound),
            )
        )
    return out


def _call_usages(
    node: ast.Call,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    bound = _binding_for_node(node.func, bindings)
    if bound is None or _in_decorator(node, parents):
        return []
    return [
        CodeUsage(
            path=rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            column=node.col_offset,
            kind=UsageKind.CALL,
            symbol=_symbol_for(node.func, bound),
            snippet=_snippet(source, node.lineno, node.end_lineno),
            content_hash=content_hash,
            is_test_code=is_test,
            bound_as=_alias_of(node.func, bindings),
            confidence=_confidence(bound),
        )
    ]


def _attribute_usages(
    node: ast.Attribute,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    bound = _binding_for_node(node, bindings)
    if bound is None:
        return []
    if _in_decorator(node, parents) or _in_class_base(node, parents):
        return []
    p = parents.get(node)
    if isinstance(p, ast.Call) and p.func is node:
        return []
    return [
        CodeUsage(
            path=rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            column=node.col_offset,
            kind=UsageKind.ATTRIBUTE,
            symbol=node.attr,
            snippet=_snippet(source, node.lineno, node.end_lineno),
            content_hash=content_hash,
            is_test_code=is_test,
            bound_as=_alias_of(node, bindings),
            confidence=_confidence(bound),
        )
    ]


def _name_usages(
    node: ast.Name,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    bound = bindings.get(node.id)
    # Whole-module imports (e.g. `import pydantic`) are referenced via Attribute
    # or Call, not as a bare symbol, so skip them here.
    if bound is None or bound.symbol is None:
        return []
    if _in_decorator(node, parents) or _in_class_base(node, parents):
        return []
    p = parents.get(node)
    if isinstance(p, ast.Call) and p.func is node:
        return []
    if isinstance(p, (ast.Import, ast.ImportFrom)):
        return []
    return [
        CodeUsage(
            path=rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            column=node.col_offset,
            kind=UsageKind.ATTRIBUTE,
            symbol=bound.symbol,
            snippet=_snippet(source, node.lineno, node.end_lineno),
            content_hash=content_hash,
            is_test_code=is_test,
            bound_as=node.id,
            confidence=_confidence(bound),
        )
    ]


def _class_base_usages(
    node: ast.ClassDef,
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    out: list[CodeUsage] = []
    for base in node.bases:
        bound = _binding_for_node(base, bindings)
        if bound is None:
            continue
        out.append(
            CodeUsage(
                path=rel,
                start_line=base.lineno,
                end_line=base.end_lineno or base.lineno,
                column=base.col_offset,
                kind=UsageKind.CLASS_BASE,
                symbol=_symbol_for(base, bound),
                snippet=_snippet(source, base.lineno, base.end_lineno),
                content_hash=content_hash,
                is_test_code=is_test,
                bound_as=_alias_of(base, bindings),
                confidence=_confidence(bound),
            )
        )
    return out


def _class_config_usages(
    node: ast.ClassDef,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    if not any(_binding_for_node(base, bindings) is not None for base in node.bases):
        return []
    out: list[CodeUsage] = []
    for child in node.body:
        if isinstance(child, ast.ClassDef) and child.name == "Config":
            out.append(_config_usage(child, source, content_hash, rel, is_test, "Config"))
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name) and target.id in ("Config", "model_config"):
                    out.append(_config_usage(child, source, content_hash, rel, is_test, target.id))
                elif isinstance(target, ast.Attribute) and target.attr in (
                    "Config",
                    "model_config",
                ):
                    out.append(
                        _config_usage(child, source, content_hash, rel, is_test, target.attr)
                    )
    return out


def _config_usage(
    child: ast.stmt,
    source: str,
    content_hash: str,
    rel: str,
    is_test: bool,
    symbol: str,
) -> CodeUsage:
    return CodeUsage(
        path=rel,
        start_line=child.lineno,
        end_line=child.end_lineno or child.lineno,
        column=child.col_offset,
        kind=UsageKind.CLASS_CONFIG,
        symbol=symbol,
        snippet=_snippet(source, child.lineno, child.end_lineno),
        content_hash=content_hash,
        is_test_code=is_test,
        bound_as=None,
        confidence="high",
    )


def _decorator_usages(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    bindings: dict[str, _Binding],
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
) -> list[CodeUsage]:
    out: list[CodeUsage] = []
    for dec in node.decorator_list:
        bound = _binding_for_node(dec, bindings)
        target: ast.expr = dec
        if bound is None and isinstance(dec, ast.Call):
            bound = _binding_for_node(dec.func, bindings)
            target = dec.func
        if bound is None:
            continue
        out.append(
            CodeUsage(
                path=rel,
                start_line=target.lineno,
                end_line=target.end_lineno or target.lineno,
                column=target.col_offset,
                kind=UsageKind.DECORATOR,
                symbol=_symbol_for(target, bound),
                snippet=_snippet(source, target.lineno, target.end_lineno),
                content_hash=content_hash,
                is_test_code=is_test,
                bound_as=_alias_of(target, bindings),
                confidence=_confidence(bound),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Dynamic imports
# --------------------------------------------------------------------------- #
def _dynamic_import(
    node: ast.Call,
    source: str,
    is_test: bool,
    content_hash: str,
    rel: str,
    canonical: str,
) -> DynamicImport | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "__import__":
        mechanism = "__import__"
    elif (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
        and func.attr == "import_module"
    ):
        mechanism = "importlib.import_module"
    else:
        return None

    resolved: str | None = None
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        resolved = node.args[0].value

    if resolved is not None and canonicalize_name(resolved.split(".")[0]) != canonical:
        # A dynamic import of an unrelated module is not evidence for our dep.
        return None

    return DynamicImport(
        path=rel,
        line=node.lineno,
        snippet=_snippet(source, node.lineno, node.end_lineno),
        mechanism=mechanism,
        resolved_name=resolved,
        is_test_code=is_test,
    )


# --------------------------------------------------------------------------- #
# Test -> production association (basic heuristic)
# --------------------------------------------------------------------------- #
def _build_test_production_links(root: Path, py_files: list[Path]) -> list[TestProductionLink]:
    links: list[TestProductionLink] = []
    seen: set[str] = set()
    for file_path in sorted(py_files):
        rel = to_posix_rel_path(root, file_path)
        if not _is_test_code(rel):
            continue
        prod = _infer_production(root, rel)
        if prod is not None and prod not in seen:
            seen.add(prod)
            links.append(
                TestProductionLink(test_path=rel, production_path=prod, matched_by="filename_stem")
            )
    return links


def _infer_production(root: Path, test_rel: str) -> str | None:
    parts = Path(test_rel).parts
    kept = [p for p in parts[:-1] if p not in TEST_DIR_SEGMENTS]
    name = parts[-1]
    if name == "conftest.py":
        return None
    if name.startswith("test_"):
        stem = name[len("test_") :]
    elif name.endswith("_test.py"):
        stem = name[: -len("_test.py")] + ".py"
    else:
        stem = name
    if not stem.endswith(".py"):
        stem = stem + ".py"
    candidates = [Path(root, stem), Path(root, "src", stem)]
    if kept:
        candidates.append(Path(root, *kept, stem))
    for cand in candidates:
        if cand.is_file():
            return to_posix_rel_path(root, cand)
    return None
