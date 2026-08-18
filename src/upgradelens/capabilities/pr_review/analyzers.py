"""Deterministic PR review analyzers (plan stage S4).

These functions need no model call -- they reuse the S3 change/repository
packages (unified-diff parsing, impact analysis, symbol/context extraction and
test intelligence). The only model-dependent step is ``pr_review`` classification,
which is performed by :func:`review_pull_request` through the model gateway (fake
mode serves a canned :class:`PRReviewReport` from ``fixtures_core``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from upgradelens.change.diff import parse_unified_diff
from upgradelens.change.impact import ChangeImpact, analyze_impact
from upgradelens.change.models import ChangeSet
from upgradelens.change.symbols import extract_symbols
from upgradelens.core.action import ActionKind, TestProposal
from upgradelens.core.finding import Finding, resolve_finding_status
from upgradelens.core.verification import VerificationResult
from upgradelens.llm.gateway import CompletionRecord, ModelGateway
from upgradelens.repository.models import CodeSymbol, RepositoryProfile
from upgradelens.repository.scan import scan_repository
from upgradelens.testing import analyze_test_gaps, select_tests

from .models import PRReviewReport
from .verifiers import pr_review_verifier

__all__ = [
    "load_change_set",
    "build_repository_context",
    "analyze_change_impact",
    "retrieve_code_context",
    "recommend_tests",
    "report_to_findings",
    "review_pull_request",
    "PRReviewResult",
]


def load_change_set(unified_diff: str) -> ChangeSet:
    """Parse a unified diff into a deterministic :class:`ChangeSet`."""
    return parse_unified_diff(unified_diff)


def build_repository_context(repo_root: str | Path) -> RepositoryProfile:
    """Static scan of ``repo_root`` -- languages, manifests, tests and symbols."""
    return scan_repository(Path(repo_root))


def analyze_change_impact(change_set: ChangeSet, repo_root: str | Path) -> ChangeImpact:
    """One-hop impact analysis over the change set and repository."""
    return analyze_impact(change_set, Path(repo_root))


def retrieve_code_context(
    repo_root: str | Path, paths: list[str] | None = None
) -> list[CodeSymbol]:
    """Extract top-level symbols for the given (or all) python files."""
    root = Path(repo_root)
    files: list[Path]
    if paths:
        files = [root / p for p in paths]
    else:
        files = [p for p in root.rglob("*.py") if ".git" not in p.parts]
    symbols: list[CodeSymbol] = []
    for path in files:
        if path.exists():
            symbols.extend(extract_symbols(path))
    return symbols


def recommend_tests(
    change_set: ChangeSet,
    impact: ChangeImpact,
    profile: RepositoryProfile,
) -> list[TestProposal]:
    """Map changed modules to existing or proposed test files (S4 + S8).

    Delegates to the horizontal test-intelligence ``select_tests`` so the PR review
    and other capabilities share one recommendation implementation. A changed
    ``foo.py`` recommends the matching ``test_foo.py`` / ``foo_test.py`` when present,
    otherwise proposes a new test file (a test-gap to be closed).
    """
    proposals: list[TestProposal] = []
    for sel in select_tests(change_set, impact, profile):
        proposals.append(
            TestProposal(
                proposal_id=sel.proposal_id,
                kind=ActionKind.TEST,
                finding_ids=[],
                title=f"Run tests covering {sel.source_path}",
                test_paths=list(sel.selected_tests),
                intended_to_fail_before_fix=False,
            )
        )
    return proposals


def produce_test_gap_findings(change_set: ChangeSet, profile: RepositoryProfile) -> list[Finding]:
    """Produce citable ``test_gap`` findings for changed files lacking tests (S8)."""
    return [g.to_finding() for g in analyze_test_gaps(change_set, profile)]


def report_to_findings(report: PRReviewReport) -> list[Finding]:
    """Convert review comments into citable :class:`Finding` objects."""
    findings: list[Finding] = []
    for comment in report.comments:
        findings.append(
            Finding(
                finding_id=comment.comment_id,
                category=comment.category.value,
                severity=comment.severity,
                confidence=comment.confidence,
                summary=comment.summary,
                detail=comment.detail,
                status=resolve_finding_status(comment.status, comment.evidence_refs),
                evidence_ids=list(comment.evidence_refs),
            )
        )
    return findings


def _build_prompt(change_set: ChangeSet, impact: ChangeImpact) -> str:
    files = ", ".join(c.path for c in change_set.files) or "(none)"
    symbols = ", ".join(s.name for s in impact.direct) or "(none)"
    return (
        f"Review the pull request.\n"
        f"Changed files: {files}\n"
        f"Changed symbols: {symbols}\n"
        f"Affected symbols: {', '.join(s.name for s in impact.impacted) or '(none)'}"
    )


@dataclass(frozen=True)
class PRReviewResult:
    """The full offline-review output of :func:`review_pull_request`."""

    change_set: ChangeSet
    impact: ChangeImpact
    report: PRReviewReport
    findings: list[Finding]
    verification: VerificationResult
    tests: list[TestProposal]
    used: CompletionRecord
    test_gap_findings: list[Finding] = field(default_factory=list)

    __test__ = False


def review_pull_request(
    *,
    repo_root: str | Path,
    unified_diff: str,
    gateway: ModelGateway,
) -> PRReviewResult:
    """Run the deterministic pipeline + one (fake-able) model classification step.

    The model node named ``"pr_review"`` returns the :class:`PRReviewReport`; in
    fake mode this is served from ``gateway``'s canned responses. Everything else
    is offline and deterministic.
    """
    change_set = load_change_set(unified_diff)
    profile = build_repository_context(repo_root)
    impact = analyze_change_impact(change_set, repo_root)
    report, used = gateway.complete_structured(
        prompt=_build_prompt(change_set, impact),
        schema=PRReviewReport,
        name="pr_review",
    )
    findings = report_to_findings(report)
    verification = pr_review_verifier(findings, change_set)
    tests = recommend_tests(change_set, impact, profile)
    test_gap_findings = produce_test_gap_findings(change_set, profile)
    return PRReviewResult(
        change_set=change_set,
        impact=impact,
        report=report,
        findings=findings,
        verification=verification,
        tests=tests,
        used=used,
        test_gap_findings=test_gap_findings,
    )
