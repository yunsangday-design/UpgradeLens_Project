"""Semgrep adapter: convert raw text / SARIF into standard SecurityFinding values.

The scanner never invents vulnerabilities; it only flags locations. Conversion
to the pipeline's :class:`~upgradelens.core.security.SecurityFinding` (with
``evidence_refs`` pointing at the flagged ``code:`` location) happens here, so
the rest of the capability treats semgrep output like any other evidence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from upgradelens.core.finding import FindingStatus
from upgradelens.core.security import (
    CWE,
    SecurityCategory,
    SecurityFinding,
    Severity,
)

from .models import DEFAULT_FP_ALLOWLIST, SEMGREP_RULES


def _in_allowlist(source: str) -> bool:
    return any(re.search(fp, source) for fp in DEFAULT_FP_ALLOWLIST)


def _scan_text(text: str, source: str) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    if _in_allowlist(source):
        return out
    for rule in SEMGREP_RULES:
        for match in rule["regex"].finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            out.append(
                SecurityFinding(
                    finding_id=f"semgrep:{rule['id']}:{source}:{line}",
                    title=rule["id"],
                    category=rule["category"],
                    cwe=rule["cwe"],
                    severity=rule["severity"],
                    confidence=0.7,
                    file_path=source,
                    line=line,
                    description=f"Matched builtin rule '{rule['id']}' at line {line}.",
                    recommendation="Review the flagged expression and remediate or exempt.",
                    evidence_refs=[f"code:{source}:{line}"],
                    status=FindingStatus.CANDIDATE,
                )
            )
    return out


def _fake_scan(repo_root: str | Path) -> list[SecurityFinding]:
    root = Path(repo_root)
    findings: list[SecurityFinding] = []
    for path in root.rglob("*.py"):
        if ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(_scan_text(text, str(path.relative_to(root))))
    return findings


def _parse_sarif(sarif: dict[str, Any]) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            rule_id = result.get("ruleId", "unknown")
            loc = (result.get("locations") or [{}])[0]
            phys = loc.get("physicalLocation", {})
            fpath = phys.get("artifactLocation", {}).get("uri", "")
            line = phys.get("region", {}).get("startLine")
            out.append(
                SecurityFinding(
                    finding_id=f"semgrep:{rule_id}:{fpath}:{line}",
                    title=str(rule_id),
                    category=SecurityCategory.MISCONFIG,
                    cwe=CWE.UNKNOWN,
                    severity=Severity.MEDIUM,
                    confidence=0.7,
                    file_path=fpath,
                    line=line,
                    description=str(result.get("message", {}).get("text", "")),
                    evidence_refs=[f"code:{fpath}:{line}"] if fpath else [],
                    status=FindingStatus.CANDIDATE,
                )
            )
    return out
