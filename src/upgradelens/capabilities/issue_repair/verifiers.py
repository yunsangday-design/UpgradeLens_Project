"""Issue-repair verifier (plan stage S6).

Deterministically confirms the proposed patch targets files that exist in the
repository. A patch to a non-existent path is rejected -- the model node cannot
propose edits to files it has not grounded in the real repo.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.core.verification import VerificationCheck, VerificationResult

from .models import IssueRepairReport

__all__ = ["verify_issue_repair"]


def verify_issue_repair(
    report: IssueRepairReport, repo_root: str | Path
) -> VerificationResult:
    """Verify the repair patch targets real repository files."""
    root = Path(repo_root)
    checks: list[VerificationCheck] = []
    for target in report.patch.target_files:
        exists = (root / target).exists()
        checks.append(
            VerificationCheck(
                name=f"patch-target:{target}",
                passed=exists,
                detail=(
                    "target file exists" if exists else "patch targets a missing file"
                ),
                evidence_id=target,
            )
        )
    passed = bool(checks) and all(c.passed for c in checks)
    summary = (
        "all patch targets exist" if passed else "patch targets missing files"
    )
    return VerificationResult(
        proposal_id="issue_repair",
        checks=checks,
        passed=passed,
        summary=summary,
        evidence_ids=[c.evidence_id for c in checks if c.evidence_id],
    )
