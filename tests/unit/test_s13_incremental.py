"""Step 13, Phase 3rd item (3.1): incremental verification reuse in the loop.

Only risks implicated by the previous round's issues are re-derived after a
remediation step; every other risk reuses the prior round verbatim.
"""

from types import SimpleNamespace

from upgradelens.agent.loop import _implicated_risk_ids, _reuse_prior_risks
from upgradelens.verify.models import (
    EvidenceStatus,
    IssueCode,
    VerificationIssue,
    VerifiedReport,
    VerifiedRisk,
)


def _risk(risk_id, *, issues=(), title=None, status=EvidenceStatus.VERIFIED):
    return VerifiedRisk(
        risk_id=risk_id,
        title=title or risk_id,
        status=status,
        severity="high",
        model_severity="high",
        issues=list(issues),
    )


def _issue():
    return VerificationIssue(code=IssueCode.NO_DOC_EVIDENCE, detail="missing doc")


def test_implicated_risk_ids_only_those_with_issues():
    report = VerifiedReport(
        verified_risks=[
            _risk("a"),
            _risk("b", issues=[_issue()]),
            _risk("c", issues=[_issue()]),
        ]
    )
    assert _implicated_risk_ids(SimpleNamespace(verified=report)) == {"b", "c"}


def test_implicated_risk_ids_empty_when_clean():
    report = VerifiedReport(verified_risks=[_risk("a"), _risk("b")])
    assert _implicated_risk_ids(SimpleNamespace(verified=report)) == set()


def test_implicated_risk_keeps_fresh_reanalysis():
    # prior: b carried an issue; fresh: b re-analysed (still flagged).
    prior = VerifiedReport(verified_risks=[_risk("a"), _risk("b", issues=[_issue()])])
    fresh = VerifiedReport(verified_risks=[_risk("a"), _risk("b", issues=[_issue()])])
    merged = _reuse_prior_risks(fresh, prior, implicated={"b"})
    by_id = {r.risk_id: r for r in merged.verified_risks}
    assert by_id["b"].issues  # remediation target stays fresh (with issue)


def test_non_implicated_clean_risk_reuses_prior_verbatim():
    # prior "a" differs from fresh "a" but both clean; reuse must keep prior.
    prior = VerifiedReport(
        verified_risks=[_risk("a", title="prior-a"), _risk("b", title="prior-b", issues=[_issue()])]
    )
    fresh = VerifiedReport(
        verified_risks=[_risk("a", title="fresh-a"), _risk("b", title="fresh-b", issues=[_issue()])]
    )
    merged = _reuse_prior_risks(fresh, prior, implicated={"b"})
    by_id = {r.risk_id: r for r in merged.verified_risks}
    # non-implicated & clean -> prior copy reused, not the fresh one
    assert by_id["a"].title == "prior-a"
    assert by_id["b"].title == "fresh-b"  # implicated -> fresh


def test_non_implicated_risk_with_new_issue_is_not_hidden():
    # fresh "a" develops a NEW issue though it was clean in prior; must stay fresh.
    prior = VerifiedReport(
        verified_risks=[_risk("a", title="prior-a"), _risk("b", issues=[_issue()])]
    )
    fresh = VerifiedReport(
        verified_risks=[
            _risk("a", title="fresh-a", issues=[_issue()]),
            _risk("b", issues=[_issue()]),
        ]
    )
    merged = _reuse_prior_risks(fresh, prior, implicated={"b"})
    by_id = {r.risk_id: r for r in merged.verified_risks}
    # "a" is not implicated but surfaced a new issue -> keep fresh (not masked)
    assert by_id["a"].title == "fresh-a"
    assert by_id["a"].issues


def test_new_risk_not_in_prior_is_kept():
    prior = VerifiedReport(verified_risks=[_risk("a")])
    fresh = VerifiedReport(verified_risks=[_risk("a"), _risk("c")])
    merged = _reuse_prior_risks(fresh, prior, implicated=set())
    assert {r.risk_id for r in merged.verified_risks} == {"a", "c"}
