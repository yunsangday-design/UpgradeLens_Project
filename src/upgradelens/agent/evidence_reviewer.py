"""EvidenceReviewerAgent: unified post-hoc verification loop (MA-3).

The supervisor fans work out to professional agents; before the result
aggregates, an :class:`EvidenceReviewerAgent` re-checks every finding against
the *evidence-grounded-review* policy and the shared verifier:

* a ``verified`` finding with no ``evidence_ids`` is downgraded to ``suspected``
  (mirrors :class:`Finding` validation, but applied uniformly across agents);
* findings are de-duplicated and merged (via :class:`ResultAggregator`);
* a single :class:`~upgradelens.core.verification.VerificationResult` summarises
  how many findings survive as verified vs. suspected.

This is the one place that turns "agent claims" into "agent evidence", so every
capability path gets the same trust bar.
"""

from __future__ import annotations

from pydantic import ValidationError

from upgradelens.agent.aggregator import ResultAggregator
from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
    RunStatus,
    TaskEnvelope,
)
from upgradelens.agent.spec import AgentRegistry, AgentSpec, default_registry
from upgradelens.core.finding import Finding
from upgradelens.core.verification import VerificationCheck, VerificationResult


def _status_value(finding: Finding) -> str:
    status = finding.status
    return status.value if hasattr(status, "value") else str(status)


def apply_evidence_policy(findings: list[Finding]) -> list[Finding]:
    """Downgrade VERIFIED findings that carry no evidence to SUSPECTED.

    Returns a new list; the input is not mutated.
    """
    out: list[Finding] = []
    for f in findings:
        if _status_value(f) == "verified" and not f.evidence_ids:
            out.append(f.model_copy(update={"status": "suspected"}))
        else:
            out.append(f)
    return out


def build_verification_result(findings: list[Finding], proposal_id: str) -> VerificationResult:
    """A verification result capturing per-finding evidence gating."""
    checks: list[VerificationCheck] = []
    verified = 0
    suspected = 0
    for f in findings:
        if _status_value(f) == "verified":
            verified += 1
            passed = True
            detail = "verified with evidence"
        else:
            suspected += 1
            passed = False
            detail = "no evidence_id" if not f.evidence_ids else "not verified"
        checks.append(
            VerificationCheck(
                name=f"evidence:{f.finding_id}",
                passed=passed,
                detail=detail,
                evidence_id=f.evidence_ids[0] if f.evidence_ids else None,
            )
        )
    return VerificationResult(
        proposal_id=proposal_id,
        checks=checks,
        summary=f"{verified} verified, {suspected} suspected (evidence-gated)",
    )


class EvidenceReviewerAgent:
    """Re-validate a set of agent results through one trust bar."""

    def review(self, ctx: AgentRunContext, prior: list[AgentResult]) -> AgentResult:
        aggregated = ResultAggregator().aggregate(ctx, prior)
        cleaned = apply_evidence_policy(aggregated.findings)
        verification = build_verification_result(cleaned, proposal_id=ctx.run_id)
        status = RunStatus.COMPLETED
        return AgentResult(
            run_id=ctx.run_id,
            parent_run_id=ctx.parent_run_id,
            agent_id=ctx.agent.agent_id,
            kind=AgentKind.EVIDENCE_REVIEWER,
            status=status,
            summary=f"evidence review: {verification.summary}",
            findings=cleaned,
            verification=verification,
            coverage=aggregated.coverage,
            cost=aggregated.cost,
            notes={
                "reviewed_results": len(prior),
                # surfaced so the supervisor / UI can ask for human input instead
                # of silently picking one side of a conflicting finding
                "conflicts": list(aggregated.notes.get("conflicts", [])),
            },
        )


def _evidence_reviewer_run(ctx: AgentRunContext, task: TaskEnvelope) -> AgentResult:
    prior: list[AgentResult] = []
    for raw in task.extra.get("prior_results", []):
        try:
            prior.append(AgentResult.model_validate(raw))
        except ValidationError:
            continue
    reviewer = EvidenceReviewerAgent()
    return reviewer.review(ctx, prior)


def register_evidence_reviewer(registry: AgentRegistry | None = None) -> None:
    """Add the evidence reviewer to the shared registry (idempotent)."""
    reg = registry or default_registry()
    if reg.get(AgentKind.EVIDENCE_REVIEWER) is None:
        reg.register(
            AgentSpec(
                agent_id="evidence_reviewer",
                kind=AgentKind.EVIDENCE_REVIEWER,
                name="Evidence Reviewer",
                run=_evidence_reviewer_run,
            )
        )


__all__ = [
    "apply_evidence_policy",
    "build_verification_result",
    "EvidenceReviewerAgent",
    "register_evidence_reviewer",
]
