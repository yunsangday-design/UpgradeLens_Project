"""Security review analyzers (plan stage S7).

Building blocks for the security-review capability:

* load a change set from a unified diff (deterministic),
* build a repository context (deterministic),
* run semgrep (deterministic in fake mode),
* check dependency CVEs (deterministic, internal table),
* call one fake-able model node (``security_review``) that returns a
  :class:`~upgradelens.core.security.SecurityReviewReport`,
* merge, verify and measure coverage.

Everything except the single model node runs offline and deterministically, so the
capability is fully exercisable in fake mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from upgradelens.change.diff import parse_unified_diff
from upgradelens.change.models import ChangeSet
from upgradelens.core.action import TestProposal
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.security import (
    CWE,
    SecurityCategory,
    SecurityFinding,
    SecurityReviewReport,
)
from upgradelens.core.verification import VerificationResult
from upgradelens.integrations.semgrep import SemgrepResult, run_semgrep
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.repository import RepositoryProfile, scan_repository
from upgradelens.testing import generate_security_regression_test

from .coverage import CoverageSummary, compute_security_coverage
from .verifiers import security_review_verifier

__all__ = [
    "SecurityReviewResult",
    "load_change_set",
    "build_repository_context",
    "run_semgrep_scan",
    "check_dependency_cves",
    "report_to_findings",
    "review_security",
]

# Internal, deterministic CVE knowledge base: package -> version spec -> note.
_CVE_DB: dict[str, dict[str, str]] = {
    "django": {"<2.2.28": "CVE-2022-28346: SQL injection in Django < 2.2.28."},
    "flask": {"<2.2.5": "CVE-2023-30861: cookie deserialization in Flask < 2.2.5."},
    "pyyaml": {"<5.4": "CVE-2020-1747: arbitrary code execution in PyYAML < 5.4."},
    "requests": {"<2.20.0": "CVE-2019-11324: credential leak on redirect in requests < 2.20.0."},
}

_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*==\s*([0-9][^\s;]*)")


@dataclass(frozen=True)
class SecurityReviewResult:
    """The full, serializable outcome of a security review."""

    review_id: str
    report: SecurityReviewReport
    findings: list[Finding]
    gate: VerificationResult
    coverage: CoverageSummary
    profile: RepositoryProfile
    change_set: ChangeSet
    used_model: bool
    model_name: str | None = None
    analysis: dict[str, Any] = field(default_factory=dict)
    test_proposals: list[TestProposal] = field(default_factory=list)

    __test__ = False


def load_change_set(unified_diff: str) -> ChangeSet:
    """Parse a unified diff into a :class:`ChangeSet` (deterministic)."""
    return parse_unified_diff(unified_diff)


def build_repository_context(repo_root: str | Path) -> RepositoryProfile:
    """Profile the repository statically (deterministic)."""
    return scan_repository(str(repo_root))


def run_semgrep_scan(repo_root: str | Path, *, fake: bool = True) -> SemgrepResult:
    """Run semgrep; in ``fake`` mode uses the deterministic regex scanner."""
    return run_semgrep(repo_root, fake=fake)


def _read_pinned_version(root: Path, name: str) -> str | None:
    req = root / "requirements.txt"
    if not req.is_file():
        return None
    text = req.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        if not m:
            continue
        if canonicalize_name(m.group(1)) == name:
            return m.group(2)
    return None


def check_dependency_cves(
    repo_root: str | Path,
    dependency: str,
    target_version: str | None = None,
) -> list[SecurityFinding]:
    """Deterministic dependency CVE check against an internal table.

    When ``target_version`` is supplied it is checked against the table; otherwise
    the pinned version is read from ``requirements.txt`` under ``repo_root``.
    """
    name = canonicalize_name(dependency) if dependency else ""
    spec_map = _CVE_DB.get(name, {})
    if not spec_map:
        return []
    root = Path(repo_root)
    version = target_version or _read_pinned_version(root, name)
    if not version:
        return []
    findings: list[SecurityFinding] = []
    for spec, note in spec_map.items():
        if SpecifierSet(spec).contains(version, prereleases=False):
            findings.append(
                SecurityFinding(
                    finding_id=f"cve:{name}:{version}",
                    title=f"vulnerable-dependency:{name}",
                    category=SecurityCategory.DEPENDENCY,
                    cwe=CWE.CWE_937,
                    severity=Severity.HIGH,
                    confidence=0.95,
                    description=note,
                    recommendation=f"Upgrade {name} to a version outside {spec}.",
                    evidence_refs=[f"dependency:{name}=={version}"],
                    status=FindingStatus.CANDIDATE,
                )
            )
    return findings


def report_to_findings(report: SecurityReviewReport, change_set: ChangeSet) -> list[Finding]:
    """Convert security findings into pipeline :class:`Finding` values."""
    findings: list[Finding] = []
    for sf in report.findings:
        findings.append(
            Finding(
                finding_id=sf.finding_id,
                category=f"security:{sf.category.value}",
                severity=sf.severity,
                confidence=sf.confidence,
                summary=sf.title,
                detail=sf.description,
                evidence_ids=list(sf.evidence_refs),
                status=FindingStatus.REJECTED if sf.false_positive else sf.status,
                requires_approval=(
                    sf.severity in (Severity.CRITICAL, Severity.HIGH) and not sf.false_positive
                ),
            )
        )
    return findings


def _build_prompt(
    change_set: ChangeSet,
    profile: RepositoryProfile,
    semgrep_count: int,
    cve_count: int,
) -> str:
    files = ", ".join(c.path for c in change_set.files) or "(none)"
    return (
        "Security-review the following change.\n"
        f"Changed files: {files}\n"
        f"Repository languages: {', '.join(lang.language for lang in profile.languages)}\n"
        f"Deterministic semgrep findings: {semgrep_count}\n"
        f"Dependency CVE hits: {cve_count}\n"
        "Return a SecurityReviewReport covering secrets, injection, and dependencies."
    )


def review_security(
    repo_root: str | Path,
    unified_diff: str,
    gateway: ModelGateway,
    *,
    dependency: str = "",
    target_version: str | None = None,
) -> SecurityReviewResult:
    """Run the full security-review capability in either mode.

    The single model node (``security_review``) is the only non-deterministic step;
    in fake mode it returns a canned :class:`SecurityReviewReport`.
    """
    repo_str = str(repo_root)
    change_set = load_change_set(unified_diff)
    profile = build_repository_context(repo_str)
    used_fake = gateway.mode == ModelMode.FAKE

    # In live mode, attempt real semgrep; degrade to fake scanner if unavailable.
    if used_fake:
        semgrep = run_semgrep_scan(repo_str, fake=True)
    else:
        try:
            semgrep = run_semgrep_scan(repo_str, fake=False)
        except RuntimeError:
            # semgrep CLI not installed — degrade gracefully.
            semgrep = run_semgrep_scan(repo_str, fake=True)
    cves = check_dependency_cves(repo_str, dependency, target_version)

    prompt = _build_prompt(change_set, profile, len(semgrep.findings), len(cves))
    model_report, _ = gateway.complete_structured(
        schema=SecurityReviewReport, name="security_review", prompt=prompt
    )

    seen: set[str] = set()
    merged: list[SecurityFinding] = []
    for sf in list(model_report.findings) + semgrep.findings + cves:
        if sf.finding_id in seen:
            continue
        seen.add(sf.finding_id)
        merged.append(sf)
    report = SecurityReviewReport(
        review_id=model_report.review_id or "security-review",
        summary=model_report.summary,
        findings=merged,
    )

    findings = report_to_findings(report, change_set)
    gate = security_review_verifier(findings, change_set)
    coverage = compute_security_coverage(findings, change_set)
    model_name = gateway.config.model if hasattr(gateway, "config") else None
    test_proposals = _security_test_proposals(report, repo_str)
    return SecurityReviewResult(
        review_id=report.review_id,
        report=report,
        findings=findings,
        gate=gate,
        coverage=coverage,
        profile=profile,
        change_set=change_set,
        used_model=not used_fake,
        model_name=model_name,
        analysis={
            "semgrep_used_fake": semgrep.used_fake,
            "semgrep_findings": len(semgrep.findings),
            "cve_findings": len(cves),
        },
        test_proposals=test_proposals,
    )


def _source_path_of(refs: list[str]) -> str:
    for ref in refs:
        match = re.search(r"([\w./-]+\.py)", ref)
        if match:
            return match.group(1)
    return ""


def _security_test_proposals(report: SecurityReviewReport, repo_root: str) -> list[TestProposal]:
    """Generate a security regression test per real (non-false-positive) finding (S8)."""
    proposals: list[TestProposal] = []
    for sf in report.findings:
        if sf.false_positive:
            continue
        src = _source_path_of(sf.evidence_refs)
        if not src:
            continue
        proposals.append(
            generate_security_regression_test(repo_root=repo_root, source_path=src, finding=sf)
        )
    return proposals
