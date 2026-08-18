"""Gap Analysis: identify untested branches, exceptions and boundaries (S8)."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.change.models import ChangeLabel, ChangeSet, FileChange
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.repository.models import RepositoryProfile

__all__ = ["TestGap", "TestGapKind", "analyze_test_gaps"]

_RAISE_RE = re.compile(r"\braise\s+[A-Za-z_]\w*")
_COMPARE_RE = re.compile(r"(<=|>=|<|>|==|!=)")


class TestGapKind(StrEnum):
    MISSING_TEST = "missing_test"
    MISSING_EXCEPTION_TEST = "missing_exception_test"
    MISSING_BOUNDARY_TEST = "missing_boundary_test"


class TestGap(BaseModel):
    """A concrete place where a test is missing."""

    model_config = ConfigDict(frozen=True)

    gap_id: str
    source_path: str
    symbol: str = ""
    kind: TestGapKind = TestGapKind.MISSING_TEST
    severity: Severity = Severity.MEDIUM
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)

    def to_finding(self) -> Finding:
        """Project this gap as a citable ``test_gap`` :class:`Finding`."""
        return Finding(
            finding_id=self.gap_id,
            category="test_gap",
            severity=self.severity,
            confidence=0.6,
            summary=self.summary,
            detail=f"Test gap ({self.kind.value}) in {self.source_path}",
            status=FindingStatus.CANDIDATE,
            evidence_ids=list(self.evidence_ids),
        )


def _added_lines(change: FileChange) -> str:
    lines: list[str] = []
    for hunk in change.hunks:
        for raw in hunk.lines:
            if raw.startswith("+"):
                lines.append(raw[1:])
    return "\n".join(lines)


def analyze_test_gaps(
    change_set: ChangeSet,
    profile: RepositoryProfile,
) -> list[TestGap]:
    """Identify changed python files that lack a corresponding test.

    A changed ``foo.py`` with no matching ``test_foo.py`` / ``foo_test.py`` yields a
    :attr:`TestGapKind.MISSING_TEST` gap; if its added lines raise exceptions or
    compare values, the finer :attr:`MISSING_EXCEPTION_TEST` /
    :attr:`MISSING_BOUNDARY_TEST` is reported instead.
    """
    existing = set(profile.tests.test_paths)
    gaps: list[TestGap] = []

    def has_test(stem: str) -> bool:
        return any(
            f"test_{stem}" in Path(t).name
            or f"{stem}_test" in Path(t).name
            or stem in Path(t).stem
            for t in existing
        )

    for change in change_set.files:
        if not change.path.endswith(".py"):
            continue
        if change.label is ChangeLabel.DELETED:
            continue
        stem = Path(change.path).stem
        if has_test(stem):
            continue
        added = _added_lines(change)
        if _RAISE_RE.search(added):
            kind = TestGapKind.MISSING_EXCEPTION_TEST
        elif _COMPARE_RE.search(added):
            kind = TestGapKind.MISSING_BOUNDARY_TEST
        else:
            kind = TestGapKind.MISSING_TEST
        gaps.append(
            TestGap(
                gap_id=f"gap:{change.path}",
                source_path=change.path,
                kind=kind,
                severity=Severity.MEDIUM,
                summary=f"No test covers {change.path} ({kind.value})",
                evidence_ids=[f"code:{change.path}:1"],
            )
        )
    return gaps
