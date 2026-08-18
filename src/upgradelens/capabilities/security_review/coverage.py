"""Security review coverage (plan stage S7).

A small deterministic coverage measure: what fraction of the changed, non-deleted
files are cited by at least one finding. Mirrors the S4 coverage gate and is
independent of any model call.
"""

from __future__ import annotations

from dataclasses import dataclass

from upgradelens.change.models import ChangeLabel, ChangeSet
from upgradelens.core.finding import Finding

__all__ = ["compute_security_coverage", "CoverageSummary"]


@dataclass(frozen=True)
class CoverageSummary:
    """Deterministic review coverage over the change set."""

    changed_files: int
    cited_files: int
    coverage: float

    __test__ = False


def compute_security_coverage(findings: list[Finding], change_set: ChangeSet) -> CoverageSummary:
    """Fraction of changed files cited by at least one finding's evidence."""
    changed = {c.path for c in change_set.files if c.label is not ChangeLabel.DELETED}
    cited: set[str] = set()
    for finding in findings:
        for eid in finding.evidence_ids:
            if eid.startswith("code:"):
                cited.add(eid[len("code:") :].rsplit(":", 1)[0])
    cited_in_change = cited & changed
    coverage = len(cited_in_change) / len(changed) if changed else 0.0
    return CoverageSummary(
        changed_files=len(changed),
        cited_files=len(cited_in_change),
        coverage=coverage,
    )
