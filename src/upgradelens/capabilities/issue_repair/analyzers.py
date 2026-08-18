"""Deterministic issue-repair analyzers (plan stage S6).

The model-dependent step is ``issue_repair`` (root-cause -> patch), performed by
:func:`repair_issue` through the model gateway (fake mode serves a canned
:class:`IssueRepairReport` from ``fixtures_core``). Everything else is offline:
issue parsing, a keyword-based root-cause locator over the repository symbols, and
a deterministic verification that the proposed patch targets real files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from upgradelens.change.symbols import extract_symbols
from upgradelens.core.action import PatchProposal, TestProposal
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.verification import VerificationResult
from upgradelens.llm.gateway import CompletionRecord, ModelGateway
from upgradelens.testing import generate_repro_test

from .models import Issue, IssueRepairReport
from .verifiers import verify_issue_repair

__all__ = [
    "load_issue",
    "locate_root_cause",
    "report_to_findings",
    "repair_issue",
    "IssueRepairResult",
]

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def load_issue(issue_text: str) -> Issue:
    """Parse a free-form issue report into a structured :class:`Issue`."""
    lines = [ln for ln in issue_text.splitlines() if ln.strip()]
    title = lines[0].lstrip("# ").strip() if lines else ""
    body = "\n".join(lines[1:]).strip()
    issue_id = "ISSUE"
    match = re.search(r"(ISSUE-\d+|\#\d+)", issue_text)
    if match:
        issue_id = match.group(1).lstrip("#")
    return Issue(issue_id=issue_id, title=title, body=body)


def locate_root_cause(issue: Issue, repo_root: str | Path) -> str:
    """Deterministic keyword scan of repository symbols for likely culprits."""
    root = Path(repo_root)
    tokens = {t for t in _TOKEN_RE.findall(issue.title + " " + issue.body)}
    tokens.discard("the")
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if ".git" in path.parts:
            continue
        for sym in extract_symbols(path):
            if sym.name in tokens:
                hits.append(f"{path.as_posix()}:{sym.name}")
    if hits:
        return "Likely root cause in: " + ", ".join(hits[:5])
    return "No symbol matched the issue keywords."


def report_to_findings(report: IssueRepairReport) -> list[Finding]:
    """Convert the repair report's root cause into a citable :class:`Finding`."""
    evidence = []
    match = re.search(r"(code:)?([\w./-]+\.py)(:\d+)?", report.root_cause)
    if match:
        evidence = [f"code:{match.group(2)}"]
    # Defensive: live models may claim VERIFIED while the root cause names no
    # .py file; degrade to CANDIDATE so the Finding validator does not reject
    # the whole report (mirrors pr_review.report_to_findings).
    status = report.status
    if status is FindingStatus.VERIFIED and not evidence:
        status = FindingStatus.CANDIDATE
    return [
        Finding(
            finding_id=f"root:{report.issue_id}",
            category="issue_repair",
            severity=Severity.HIGH,
            confidence=0.7,
            summary=report.root_cause,
            detail=report.summary,
            status=status,
            evidence_ids=evidence,
        )
    ]


def _build_prompt(issue: Issue, root_cause: str) -> str:
    return (
        f"Repair issue {issue.issue_id}: {issue.title}\n"
        f"{issue.body}\n\n"
        f"Deterministic root-cause hint: {root_cause}"
    )


@dataclass(frozen=True)
class IssueRepairResult:
    issue: Issue
    report: IssueRepairReport
    findings: list[Finding]
    actions: list[PatchProposal]
    verification: VerificationResult
    used: CompletionRecord
    repro_tests: list[TestProposal] = field(default_factory=list)

    __test__ = False


def repair_issue(
    *,
    repo_root: str | Path,
    issue_text: str,
    gateway: ModelGateway,
) -> IssueRepairResult:
    """Run the deterministic pipeline + one (fake-able) model repair step."""
    issue = load_issue(issue_text)
    root_cause = locate_root_cause(issue, repo_root)
    report, used = gateway.complete_structured(
        prompt=_build_prompt(issue, root_cause),
        schema=IssueRepairReport,
        name="issue_repair",
    )
    findings = report_to_findings(report)
    actions = [report.patch] if report.patch.target_files else []
    verification = verify_issue_repair(report, repo_root)
    repro_tests = _repro_tests_for(report, repo_root, issue_text)
    return IssueRepairResult(
        issue=issue,
        report=report,
        findings=findings,
        actions=actions,
        verification=verification,
        used=used,
        repro_tests=repro_tests,
    )


def _repro_tests_for(
    report: IssueRepairReport, repo_root: str | Path, issue_text: str
) -> list[TestProposal]:
    """Generate a reproduction test for the reported issue (S8 horizontal hook)."""
    src = ""
    match = re.search(r"(code:)?([\w./-]+\.py)(:\d+)?", report.root_cause)
    if match:
        src = match.group(2)
    else:
        for p in Path(repo_root).rglob("*.py"):
            if ".git" in p.parts:
                continue
            if p.name.startswith("test_") or p.name.endswith("_test.py"):
                continue
            src = p.as_posix()
            break
    if not src:
        return []
    return [
        generate_repro_test(
            repo_root=repo_root,
            source_path=src,
            issue_text=issue_text,
        )
    ]
