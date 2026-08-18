"""Security gate (plan stage S7).

Thin wrapper that runs the security-review verifier and returns a
:class:`~upgradelens.core.verification.VerificationResult`. This is the ``verify/``
hook the security-review capability plugs into, so the gate is exercised uniformly
with the other capability verifiers.
"""

from __future__ import annotations

from upgradelens.capabilities.security_review.verifiers import security_review_verifier
from upgradelens.change.models import ChangeSet
from upgradelens.core.finding import Finding
from upgradelens.core.verification import VerificationResult

__all__ = ["verify_security"]


def verify_security(findings: list[Finding], change_set: ChangeSet) -> VerificationResult:
    """Run the security verification gate over a set of findings."""
    return security_review_verifier(findings, change_set)
