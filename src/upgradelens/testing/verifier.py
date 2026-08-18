"""Test Verification: ensure a proposed test is non-trivial and fails-before/passes-after (S8)."""

from __future__ import annotations

import re

from upgradelens.core.action import TestProposal
from upgradelens.core.verification import VerificationCheck, VerificationResult

__all__ = ["verify_test_proposal", "ASSERTION_RE", "TRIVIAL_ASSERT_RE"]

ASSERTION_RE = re.compile(r"\bassert\b")
# Trivial / always-true assertions, treated as an "empty" test.
TRIVIAL_ASSERT_RE = re.compile(
    r"assert\s+(True|False|1\s*==\s*1|0\s*==\s*0|1\s*!=\s*0|False is False|True is True)\b",
    re.IGNORECASE,
)


def verify_test_proposal(
    proposal: TestProposal,
    *,
    runnable: bool = False,
    before_pass: bool | None = None,
    after_pass: bool | None = None,
) -> VerificationResult:
    """Verify a proposed test.

    * Static: the test must contain a real (non-trivial) assertion -- empty asserts
      or ``assert True`` are rejected.
    * Execution (only when ``runnable``): a regression/repro test must FAIL on the
      unpatched code (``before_pass is False``) and PASS after the fix
      (``after_pass is True``).
    * If execution cannot be performed, the test is marked ``proposed``, never
      ``verified`` -- so ``passed`` stays ``False``.
    """
    test_code = (proposal.metadata or {}).get("test_code", "")
    has_assert = bool(ASSERTION_RE.search(test_code))
    trivial = bool(TRIVIAL_ASSERT_RE.search(test_code))
    static_ok = has_assert and not trivial

    evidence_id = proposal.test_paths[0] if proposal.test_paths else None
    checks: list[VerificationCheck] = [
        VerificationCheck(
            name=f"static_non_trivial:{proposal.proposal_id}",
            passed=static_ok,
            detail=(
                "test contains a real assertion"
                if static_ok
                else "test is empty or uses a trivial/always-true assertion"
            ),
            evidence_id=evidence_id,
        )
    ]

    if runnable:
        before_fails = before_pass is False
        after_ok = after_pass is True
        exec_ok = static_ok and before_fails and after_ok
        checks.append(
            VerificationCheck(
                name=f"execution:{proposal.proposal_id}",
                passed=exec_ok,
                detail=(
                    "fails before fix and passes after fix"
                    if exec_ok
                    else "execution result does not satisfy fail-before/pass-after"
                ),
                evidence_id=evidence_id,
            )
        )
        passed = bool(checks) and all(c.passed for c in checks)
        summary = (
            "verified: test fails before the fix and passes after"
            if passed
            else "verification failed: execution result not satisfied"
        )
    else:
        # Cannot execute -> never verified, only proposed (if static-valid).
        checks.append(
            VerificationCheck(
                name=f"execution:{proposal.proposal_id}",
                passed=False,
                detail="test not executed; marked proposed (cannot be verified offline)",
                evidence_id=evidence_id,
            )
        )
        passed = False
        summary = (
            "proposed: non-trivial test, not executed"
            if static_ok
            else "invalid: test is empty or trivial; cannot be proposed"
        )

    return VerificationResult(
        proposal_id=proposal.proposal_id,
        checks=checks,
        passed=passed,
        summary=summary,
        evidence_ids=[c.evidence_id for c in checks if c.evidence_id],
    )
