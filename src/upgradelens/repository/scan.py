"""Scan a repository into a :class:`RepositoryProfile` (plan stage S3).

Deterministic filesystem walk: language stats by extension, known dependency manifests
(with dependency extraction), test locations, and Python top-level symbols. No model,
no network.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from upgradelens.change.symbols import extract_symbols
from upgradelens.repository.models import (
    CodeSymbol,
    LanguageProfile,
    ManifestInfo,
    RepositoryProfile,
    TestProfile,
)

__all__ = ["scan_repository"]

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    "site-packages",
}

_EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
}


def _is_test_path(path: Path) -> bool:
    parts = path.parts
    if "test" in parts or "tests" in parts:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.go")


def _parse_requirements(text: str) -> list[str]:
    deps: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Strip environment markers and version specifiers.
        name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0]
        name = name.split(";")[0].strip()
        if name:
            deps.append(name)
    return deps


def _manifest_for(path: Path) -> ManifestInfo | None:
    name = path.name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return ManifestInfo(path=str(path), ecosystem="pypi")
        proj_deps: list[str] = []
        proj = data.get("project", {})
        proj_deps.extend(proj.get("dependencies", []) or [])
        poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        proj_deps.extend(k for k in poetry if k != "python")
        return ManifestInfo(path=str(path), ecosystem="pypi", dependencies=proj_deps)
    if name == "requirements.txt" or name.endswith(".txt") and "requirement" in path.name:
        return ManifestInfo(
            path=str(path), ecosystem="pypi", dependencies=_parse_requirements(text)
        )
    if name == "package.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ManifestInfo(path=str(path), ecosystem="npm")
        pkg_deps = list((data.get("dependencies") or {}).keys())
        pkg_deps += list((data.get("devDependencies") or {}).keys())
        return ManifestInfo(path=str(path), ecosystem="npm", dependencies=pkg_deps)
    if name == "go.mod":
        gomod_deps: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("require ("):
                continue
            if line.startswith("require ") and " " in line[8:]:
                gomod_deps.append(line[8:].split()[0])
        return ManifestInfo(path=str(path), ecosystem="gomod", dependencies=gomod_deps)
    if name == "Pipfile":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return ManifestInfo(path=str(path), ecosystem="pipenv")
        pip_deps = list((data.get("packages") or {}).keys())
        return ManifestInfo(path=str(path), ecosystem="pipenv", dependencies=pip_deps)
    return None


def scan_repository(root: str | Path) -> RepositoryProfile:
    """Walk ``root`` and build its static profile."""
    root_path = Path(root)
    lang_counts: dict[str, list[int]] = {}  # language -> [file_count, loc]
    manifests: list[ManifestInfo] = []
    test_paths: list[str] = []
    symbols: list[CodeSymbol] = []

    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        ext = path.suffix.lower()
        lang = _EXT_LANG.get(ext)
        if lang:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            stats = lang_counts.setdefault(lang, [0, 0])
            stats[0] += 1
            stats[1] += len(lines)
            if lang == "python":
                symbols.extend(extract_symbols(path))
        manifest = _manifest_for(path)
        if manifest is not None:
            manifests.append(manifest)
        if _is_test_path(path):
            test_paths.append(str(path))

    languages = [
        LanguageProfile(language=lang, file_count=c, loc=loc)
        for lang, (c, loc) in sorted(lang_counts.items())
    ]
    framework = "pytest" if (root_path / "pytest.ini").exists() else ""
    tests = TestProfile(test_paths=sorted(test_paths), framework=framework)
    return RepositoryProfile(
        root=str(root_path),
        languages=languages,
        manifests=manifests,
        tests=tests,
        symbols=symbols,
    )
