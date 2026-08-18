"""Semgrep integration data types and SARIF projection (plan stage S7).

Holds the structured result of a semgrep run plus the rule tables and the
optional SARIF projection used by the security-review capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from upgradelens.core.security import (
    CWE,
    SecurityCategory,
    SecurityFinding,
    Severity,
)

__all__ = [
    "SemgrepResult",
    "SEMGREP_RULES",
    "DEFAULT_FP_ALLOWLIST",
    "ALLOWED_CONFIGS",
    "SUPPORTED_SEMGREP_VERSION",
    "MAX_OUTPUT_BYTES",
    "to_sarif",
]

# Fixed, supported semgrep version range (documented constraint).
SUPPORTED_SEMGREP_VERSION = ">=1.0.0"
# Only these rule sets may ever be passed to the CLI -- see runner.run_semgrep.
ALLOWED_CONFIGS: tuple[str, ...] = ("auto", "p/ci", "p/security-audit")
# Guard against runaway semgrep output (bytes).
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class SemgrepResult:
    """The outcome of a semgrep run."""

    findings: list[SecurityFinding]
    sarif: dict[str, Any] | None = None
    used_fake: bool = False


# Builtin regex rules used by the deterministic fake scanner.
_SECRETS_RE = re.compile(
    r"""(?i)(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['"][A-Za-z0-9_.\-]{8,}['"]"""
)
_SQLI_RE = re.compile(
    r"""(?i)(?:execute|cursor\.execute|raw\()\s*\(?[^)]*?\b(?:select|insert|update|delete)\b"""
)
_DANGER_RE = re.compile(
    r"""(?i)(?:eval\(|os\.system\(|subprocess\.(?:call|run|popen)\([^)]*shell\s*=\s*True)"""
)

SEMGREP_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "hardcoded-secret",
        "regex": _SECRETS_RE,
        "category": SecurityCategory.SECRET,
        "cwe": CWE.CWE_259,
        "severity": Severity.HIGH,
    },
    {
        "id": "sql-injection",
        "regex": _SQLI_RE,
        "category": SecurityCategory.INJECTION,
        "cwe": CWE.CWE_89,
        "severity": Severity.HIGH,
    },
    {
        "id": "command-injection",
        "regex": _DANGER_RE,
        "category": SecurityCategory.INJECTION,
        "cwe": CWE.CWE_78,
        "severity": Severity.CRITICAL,
    },
)

# Path fragments that exempt a finding from the gate (tests, fixtures, examples).
DEFAULT_FP_ALLOWLIST: tuple[str, ...] = (
    r"(^|/)tests?/",
    r"(^|/)\.github/",
    r"(^|/)examples?/",
    r"(^|/)fixtures?/",
)


def to_sarif(result: SemgrepResult) -> dict[str, Any]:
    """Project findings into a minimal SARIF 2.1.0 document (optional output).

    Used when callers want to emit the security-review result in the SARIF
    format that real ``semgrep`` produces, so downstream tooling can ingest it.
    """
    level_map = {
        Severity.CRITICAL: "error",
        Severity.HIGH: "error",
        Severity.MEDIUM: "warning",
        Severity.LOW: "note",
        Severity.INFO: "none",
    }
    sarif_results: list[dict[str, Any]] = []
    for f in result.findings:
        location = (
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": f.file_path},
                    "region": {"startLine": f.line or 0},
                }
            }
            if f.file_path
            else {}
        )
        sarif_results.append(
            {
                "ruleId": f.finding_id,
                "level": level_map.get(f.severity, "warning"),
                "message": {"text": f.description or f.title},
                "locations": [location] if location else [],
            }
        )
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "upgradelens-security-review",
                        "semanticVersion": SUPPORTED_SEMGREP_VERSION,
                    }
                },
                "results": sarif_results,
            }
        ],
    }
