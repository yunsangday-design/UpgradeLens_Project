"""Security review data models (plan stage S7).

Re-exports the security domain types defined in
:mod:`upgradelens.core.security` so the package has a single import surface,
mirroring :mod:`upgradelens.capabilities.pr_review.models`.
"""

from __future__ import annotations

from upgradelens.core.security import (
    CWE,
    SecurityCategory,
    SecurityFinding,
    SecurityReviewReport,
)

__all__ = [
    "CWE",
    "SecurityCategory",
    "SecurityFinding",
    "SecurityReviewReport",
]
