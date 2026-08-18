"""Tests for EvidenceReviewerAgent (MA-3) and DynamicDecomposer (MA-4)."""

from __future__ import annotations

from upgradelens.agent.decomposer import DynamicDecomposer
from upgradelens.agent.evidence_reviewer import (
    EvidenceReviewerAgent,
    apply_evidence_policy,
    build_verification_result,
)
from upgradelens.agent.runtime import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentRunContext,
    RunStatus,
    TaskEnvelope,
    new_run_id,
)
from upgradelens.agent.spec import AgentRegistry, AgentSpec
from upgradelens.core.finding import Finding, Severity


def _ctx() -> AgentRunContext:
    return AgentRunContext(
        run_id=new_run_id(),
        agent=AgentIdentity.create(AgentKind.SUPERVISOR),
    )


def _finding(fid: str, sev: Severity, status: str, evidence: bool) -> Finding:
    # model_construct bypasses validation so we can simulate the (invalid) case
    # of a VERIFIED finding without evidence_ids -- exactly what the policy guards.
    return Finding.model_construct(
        finding_id=fid,
        category="c",
        severity=sev,
        confidence=0.9,
        summary=f"f {fid}",
        evidence_ids=["e1"] if evidence else [],
        status=status,
    )


def test_apply_evidence_policy_downgrades_unverified() -> None:
    findings = [
        _finding("a", Severity.HIGH, "verified", evidence=True),
        _finding("b", Severity.HIGH, "verified", evidence=False),
        _finding("c", Severity.LOW, "suspected", evidence=False),
    ]
    cleaned = apply_evidence_policy(findings)
    by_id = {f.finding_id: f for f in cleaned}
    assert by_id["a"].status == "verified"
    assert by_id["b"].status == "suspected"  # downgraded
    assert by_id["c"].status == "suspected"


def test_build_verification_result_counts() -> None:
    findings = [
        _finding("a", Severity.HIGH, "verified", evidence=True),
        _finding("b", Severity.HIGH, "verified", evidence=False),
    ]
    # policy runs before verification, so b is downgraded to suspected
    cleaned = apply_evidence_policy(findings)
    vr = build_verification_result(cleaned, proposal_id="p1")
    assert vr.proposal_id == "p1"
    # one verified (a), one suspected (b) -> not all passed
    assert vr.passed is False
    assert "1 verified" in vr.summary


def test_evidence_reviewer_review_pipeline() -> None:
    reviewer = EvidenceReviewerAgent()
    prior = [
        AgentResult(
            run_id=new_run_id(),
            agent_id="pr",
            kind=AgentKind.PR_REVIEW,
            findings=[_finding("x", Severity.HIGH, "verified", evidence=True)],
        ),
        AgentResult(
            run_id=new_run_id(),
            agent_id="sec",
            kind=AgentKind.SECURITY_REVIEW,
            findings=[_finding("y", Severity.MEDIUM, "verified", evidence=False)],
        ),
    ]
    result = reviewer.review(_ctx(), prior)
    assert result.kind is AgentKind.EVIDENCE_REVIEWER
    assert result.verification is not None
    ids = {f.finding_id for f in result.findings}
    assert ids == {"x", "y"}
    # y was verified-without-evidence -> downgraded to suspected
    y = next(f for f in result.findings if f.finding_id == "y")
    assert y.status == "suspected"


def _fake_registry() -> AgentRegistry:
    reg = AgentRegistry()

    def _agent(kind: AgentKind):
        def run(ctx, task):
            return AgentResult(
                run_id=ctx.run_id,
                parent_run_id=ctx.parent_run_id,
                agent_id=ctx.agent.agent_id,
                kind=kind,
                status=RunStatus.COMPLETED,
                summary=f"ran {task.kind}",
            )

        return AgentSpec(agent_id=kind.value, kind=kind, name=kind.value, run=run)

    for kind in (
        AgentKind.PR_REVIEW,
        AgentKind.SECURITY_REVIEW,
        AgentKind.DEPENDENCY_UPGRADE,
        AgentKind.EVIDENCE_REVIEWER,
    ):
        reg.register(_agent(kind))
    return reg


def test_decomposer_full_audit_fan_out_fan_in() -> None:
    reg = _fake_registry()
    dec = DynamicDecomposer(reg)
    plan = dec.decompose(TaskEnvelope(kind="full_audit", extra={"has_dependencies": True}))
    assert set(plan.steps) == {
        "leaf_pr_review",
        "leaf_security_review",
        "leaf_dependency_upgrade",
        "evidence_review",
    }
    waves = []
    # reconstruct waves via the public scheduler import
    from upgradelens.agent.execution_plan import execution_waves

    waves = execution_waves(plan)
    assert len(waves[0]) == 3  # fan-out
    assert waves[1] == ["evidence_review"]  # fan-in


def test_decomposer_single_kind_constrained() -> None:
    reg = _fake_registry()
    dec = DynamicDecomposer(reg)
    plan = dec.decompose(TaskEnvelope(kind="pr_review"))
    leaves = [s for s in plan.steps if s != "evidence_review"]
    assert len(leaves) == 1
    assert "leaf_pr_review" in plan.steps


def test_decomposer_and_run_runs_leaves_then_reviewer() -> None:
    reg = _fake_registry()
    dec = DynamicDecomposer(reg)
    reviewed, plan = dec.decompose_and_run(
        TaskEnvelope(kind="full_audit", extra={"has_dependencies": True}), _ctx()
    )
    assert reviewed.kind is AgentKind.EVIDENCE_REVIEWER
    assert reviewed.notes["reviewed_results"] == 3
