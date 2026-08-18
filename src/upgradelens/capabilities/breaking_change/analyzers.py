"""Deterministic breaking-change analyzers (plan stage S5).

Reuses the S3 change/repository packages for symbol extraction and diff parsing.
The model-dependent step is ``breaking_change`` classification, performed by
:func:`review_breaking_changes` through the model gateway (fake mode serves a
canned :class:`BreakingChangeReport` from ``fixtures_core``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from upgradelens.change.diff import parse_unified_diff
from upgradelens.change.models import ChangeLabel, ChangeSet
from upgradelens.change.symbols import extract_symbols
from upgradelens.core.finding import Finding
from upgradelens.core.verification import VerificationResult
from upgradelens.llm.gateway import CompletionRecord, ModelGateway
from upgradelens.repository.models import CodeSymbol

from .models import ApiChangeKind, BreakingChange, BreakingChangeReport
from .verifiers import verify_breaking_changes

__all__ = [
    "extract_public_symbols",
    "compare_versions",
    "classify_api_change",
    "detect_breaking_changes",
    "report_to_findings",
    "review_breaking_changes",
    "BreakingChangeResult",
    "VersionComparison",
]


def extract_public_symbols(repo_root: str | Path) -> list[CodeSymbol]:
    """Top-level, non-private symbols defined in the repository."""
    root = Path(repo_root)
    symbols: list[CodeSymbol] = []
    for path in root.rglob("*.py"):
        if ".git" in path.parts:
            continue
        symbols.extend(s for s in extract_symbols(path) if not s.name.startswith("_"))
    return symbols


def compare_versions(from_version: str, to_version: str) -> VersionComparison:
    """Classify the upgrade magnitude as major / minor / patch / none."""

    def _parts(v: str) -> tuple[int, int, int]:
        digits = [p for p in v.replace("v", "").split(".") if p.isdigit()]
        nums = [int(d) for d in digits[:3]]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2])

    a, b = _parts(from_version), _parts(to_version)
    if a[0] != b[0]:
        level = "major"
    elif a[1] != b[1]:
        level = "minor"
    elif a[2] != b[2]:
        level = "patch"
    else:
        level = "none"
    return VersionComparison(from_version=from_version, to_version=to_version, level=level)


@dataclass(frozen=True)
class VersionComparison:
    from_version: str
    to_version: str
    level: str

    __test__ = False


def classify_api_change(symbol: str, old_signature: str, new_signature: str) -> ApiChangeKind:
    """Heuristic API-change classifier (deterministic, no model)."""
    if new_signature == "":
        return ApiChangeKind.DELETION
    old_name = old_signature.split("(", 1)[0].strip()
    new_name = new_signature.split("(", 1)[0].strip()
    if old_name and new_name and old_name != new_name:
        return ApiChangeKind.RENAME
    if old_signature != new_signature:
        return ApiChangeKind.SIGNATURE_CHANGE
    return ApiChangeKind.BEHAVIOR_CHANGE


_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)")


def _changed_symbols(file_change: object) -> set[str]:
    """Symbols defined on added lines of a file's hunks."""
    syms: set[str] = set()
    for hunk in getattr(file_change, "hunks", []):
        for line in getattr(hunk, "lines", []):
            if not line.startswith("+"):
                continue
            text = line[1:]
            match = _DEF_RE.match(text) or _CLASS_RE.match(text)
            if match:
                syms.add(match.group(1))
    return syms


def detect_breaking_changes(change_set: ChangeSet, repo_root: str | Path) -> list[BreakingChange]:
    """Deterministic pre-filter: changed public symbols become candidates.

    The model node performs the authoritative classification; this step only
    surfaces the symbols worth asking about.
    """
    public = {s.name for s in extract_public_symbols(repo_root)}
    candidates: list[BreakingChange] = []
    seen: set[str] = set()
    for change in change_set.files:
        if change.label in (ChangeLabel.DELETED, ChangeLabel.BINARY):
            continue
        for symbol in _changed_symbols(change):
            if symbol not in public or symbol in seen:
                continue
            seen.add(symbol)
            candidates.append(
                BreakingChange(
                    change_id=f"bc:{symbol}",
                    kind=ApiChangeKind.BEHAVIOR_CHANGE,
                    symbol=symbol,
                    summary=f"Public symbol '{symbol}' changed.",
                )
            )
    return candidates


def report_to_findings(report: BreakingChangeReport) -> list[Finding]:
    """Convert breaking changes into citable :class:`Finding` objects."""
    findings: list[Finding] = []
    for change in report.changes:
        findings.append(
            Finding(
                finding_id=change.change_id,
                category="breaking_change",
                severity=change.severity,
                confidence=change.confidence,
                summary=change.summary,
                detail=change.detail,
                status=change.status,
                evidence_ids=list(change.evidence_refs),
            )
        )
    return findings


def _build_prompt(change_set: ChangeSet, comparison: VersionComparison) -> str:
    files = ", ".join(c.path for c in change_set.files) or "(none)"
    return (
        f"Detect breaking changes for upgrade {comparison.from_version} -> "
        f"{comparison.to_version} ({comparison.level}).\n"
        f"Changed files: {files}"
    )


@dataclass(frozen=True)
class BreakingChangeResult:
    change_set: ChangeSet
    comparison: VersionComparison
    report: BreakingChangeReport
    findings: list[Finding]
    verification: VerificationResult
    used: CompletionRecord

    __test__ = False


def review_breaking_changes(
    *,
    repo_root: str | Path,
    unified_diff: str,
    from_version: str,
    to_version: str,
    gateway: ModelGateway,
) -> BreakingChangeResult:
    """Run the deterministic pipeline + one (fake-able) model classification step."""
    change_set = parse_unified_diff(unified_diff)
    comparison = compare_versions(from_version, to_version)
    report, used = gateway.complete_structured(
        prompt=_build_prompt(change_set, comparison),
        schema=BreakingChangeReport,
        name="breaking_change",
    )
    findings = report_to_findings(report)
    verification = verify_breaking_changes(findings, change_set)
    return BreakingChangeResult(
        change_set=change_set,
        comparison=comparison,
        report=report,
        findings=findings,
        verification=verification,
        used=used,
    )
