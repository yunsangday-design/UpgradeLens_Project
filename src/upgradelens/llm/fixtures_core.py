"""Pre-generated fake responses for the generic capability nodes (S1-S9).

The whole extended agent runtime is driven by ``ModelGateway`` in ``fake`` mode:
every LLM-touching node is keyed to a canned :class:`pydantic.BaseModel` response
so the pipeline can be exercised end-to-end **without any real model call**. This
module collects those canned responses in one place; capabilities register their
node names here so tests and offline demos stay reproducible.

Usage::

    gw = ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )
    report, used = gw.complete_structured(prompt=..., schema=ImpactReport, name="analyse")
"""

from __future__ import annotations

from typing import Any

from upgradelens.capabilities.breaking_change.models import (
    ApiChangeKind,
    BreakingChange,
    BreakingChangeReport,
)
from upgradelens.capabilities.issue_repair.models import IssueRepairReport
from upgradelens.capabilities.pr_review.models import (
    PRReviewReport,
    ReviewCategory,
    ReviewComment,
)
from upgradelens.core.action import PatchProposal
from upgradelens.core.finding import FindingStatus, Severity
from upgradelens.core.security import (
    CWE,
    SecurityCategory,
    SecurityFinding,
    SecurityReviewReport,
)
from upgradelens.models.impact import ImpactReport, RiskItem

__all__ = ["build_fake_core_responses"]


def _canned_impact_report() -> ImpactReport:
    """A minimal, self-consistent dependency-upgrade impact report."""
    return ImpactReport(
        target_dependency="pydantic",
        source_version_spec="1.10",
        target_version_spec="2.0",
        risks=[
            RiskItem(
                risk_id="r1",
                title=".dict() removed in 2.0",
                severity="high",
                confidence="high",
                evidence_ids=["code-1"],
                recommendation="Use model_dump() instead of .dict().",
            )
        ],
        evidence_summary={"code_usage": 1},
    )


def _canned_pr_review_report() -> PRReviewReport:
    """A pre-generated PR review classification, keyed to ``"pr_review"``.

    Cites ``src/app.py`` so the offline verifier (which requires verified findings
    to point at real changed code) passes in the PR-review integration test; the
    test repository is built to contain that file.
    """
    return PRReviewReport(
        review_id="rev-001",
        pr_title="Refactor app entrypoint",
        comments=[
            ReviewComment(
                comment_id="c1",
                category=ReviewCategory.LOGIC_RISK,
                severity=Severity.HIGH,
                confidence=0.85,
                file_path="src/app.py",
                line=12,
                summary="Removed null check may dereference None",
                detail="The refactor dropped the guard before dereferencing the result.",
                recommendation="Re-add the None guard before use.",
                evidence_refs=["code:src/app.py:12"],
                status=FindingStatus.VERIFIED,
            ),
            ReviewComment(
                comment_id="c2",
                category=ReviewCategory.TEST_GAP,
                severity=Severity.MEDIUM,
                confidence=0.7,
                file_path="src/app.py",
                line=30,
                summary="New branch has no covering test",
                detail="The new error path is not exercised by any test.",
                recommendation="Add tests/test_app.py covering the new branch.",
                evidence_refs=["test:tests/test_app.py"],
                status=FindingStatus.CANDIDATE,
            ),
            ReviewComment(
                comment_id="c3",
                category=ReviewCategory.COMPATIBILITY,
                severity=Severity.LOW,
                confidence=0.6,
                file_path="src/app.py",
                line=30,
                summary="Uses an API only available on py3.11+",
                detail="The call relies on a stdlib addition newer than the stated floor.",
                recommendation="Guard with a sys.version_info check.",
                evidence_refs=["code:src/app.py:30"],
                status=FindingStatus.VERIFIED,
            ),
        ],
        summary="3 review items: 2 verified, 1 test gap.",
    )


def _canned_breaking_change_report() -> BreakingChangeReport:
    """A pre-generated breaking-change classification, keyed to ``"breaking_change"``.

    Cites ``src/app.py`` so the offline verifier (verified breaks must point at real
    changed code) passes in the breaking-change integration test.
    """
    return BreakingChangeReport(
        report_id="bc-001",
        from_version="1.10",
        to_version="2.0",
        changes=[
            BreakingChange(
                change_id="bc:model_dump",
                kind=ApiChangeKind.SIGNATURE_CHANGE,
                severity=Severity.HIGH,
                confidence=0.9,
                symbol="model_dump",
                old_signature="def model_dump()",
                new_signature="def model_dump(*, exclude=None)",
                summary="model_dump gained a required keyword-only argument",
                detail="Callers passing positional args will break.",
                recommendation="Pass exclude as a keyword argument.",
                evidence_refs=["code:src/app.py:12"],
                status=FindingStatus.VERIFIED,
            ),
            BreakingChange(
                change_id="bc:old_helper",
                kind=ApiChangeKind.DELETION,
                severity=Severity.CRITICAL,
                confidence=0.8,
                symbol="old_helper",
                old_signature="def old_helper()",
                new_signature="",
                summary="old_helper was removed in 2.0",
                detail="No replacement is provided.",
                recommendation="Inline the helper or vendor it.",
                evidence_refs=["code:src/app.py:30"],
                status=FindingStatus.VERIFIED,
            ),
        ],
        summary="2 breaking changes detected for 1.10 -> 2.0.",
    )


def _canned_issue_repair_report() -> IssueRepairReport:
    """A pre-generated issue repair, keyed to ``"issue_repair"``.

    The patch targets ``src/app.py`` so the offline verifier (patch must target a
    real file) passes in the issue-repair integration test.
    """
    patch_diff = (
        "--- a/src/app.py\n+++ b/src/app.py\n"
        "@@\n-    return cfg.value\n+    return cfg.value if cfg else DEFAULT\n"
    )
    return IssueRepairReport(
        report_id="rep-001",
        issue_id="ISSUE-42",
        root_cause="Null dereference when config is missing in src/app.py:handle().",
        patch=PatchProposal(
            proposal_id="patch:app",
            diff=patch_diff,
            target_files=["src/app.py"],
        ),
        suggested_tests=["tests/test_app.py"],
        summary="Guard the missing-config path before dereferencing.",
        status=FindingStatus.VERIFIED,
    )


def _canned_security_review_report() -> SecurityReviewReport:
    """A pre-generated security review, keyed to ``"security_review"``.

    The finding is verified and cites ``src/app.py`` so the offline security gate
    passes in the security-review integration test (the change set must include
    that path).
    """
    return SecurityReviewReport(
        review_id="sec-001",
        summary="One high-severity hardcoded secret; cites src/app.py.",
        findings=[
            SecurityFinding(
                finding_id="sec:hardcoded-secret:src/app.py:42",
                title="hardcoded-secret",
                category=SecurityCategory.SECRET,
                cwe=CWE.CWE_259,
                severity=Severity.HIGH,
                confidence=0.9,
                file_path="src/app.py",
                line=42,
                description="A long-lived secret is hard-coded in source.",
                recommendation="Move the secret to configuration / a secret manager.",
                evidence_refs=["code:src/app.py:42"],
                status=FindingStatus.VERIFIED,
            ),
        ],
    )


def build_fake_core_responses() -> dict[str, Any]:
    """Return canned node-name -> BaseModel responses for the generic runtime.

    Seeded nodes so far: ``analyse`` / ``impact_analyzer`` (dependency upgrade),
    ``pr_review`` (PR review), ``breaking_change`` (API break detection),
    ``issue_repair`` (automated fix) and ``security_review`` (security review).
    Each capability may add more as it lands.
    """
    impact = _canned_impact_report()
    return {
        "analyse": impact,
        "impact_analyzer": impact,
        "pr_review": _canned_pr_review_report(),
        "breaking_change": _canned_breaking_change_report(),
        "issue_repair": _canned_issue_repair_report(),
        "security_review": _canned_security_review_report(),
    }
