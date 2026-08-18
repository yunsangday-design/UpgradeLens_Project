"""Semgrep integration package (plan stage S7).

Runs ``semgrep`` (or a deterministic offline scanner) and parses its SARIF
output into :class:`~upgradelens.core.security.SecurityFinding` values. The
public surface mirrors the former single-module API so call sites only import
from ``upgradelens.integrations.semgrep``.
"""

from __future__ import annotations

from .models import (
    ALLOWED_CONFIGS,
    DEFAULT_FP_ALLOWLIST,
    SEMGREP_RULES,
    SUPPORTED_SEMGREP_VERSION,
    SemgrepResult,
    to_sarif,
)
from .runner import check_semgrep_available, run_semgrep

__all__ = [
    "SemgrepResult",
    "run_semgrep",
    "SEMGREP_RULES",
    "DEFAULT_FP_ALLOWLIST",
    "ALLOWED_CONFIGS",
    "SUPPORTED_SEMGREP_VERSION",
    "to_sarif",
    "check_semgrep_available",
]
