"""ROADMAP Step 5 -- Verifier feedback -> re-planning loop.

These tests drive ``_run_verification_loop`` directly with a *patched* analyse
function, so the loop is fully offline and deterministic ("第一次失败、第二次成功"
replayed without any model or doc store). They cover every branch the S5 work
items and acceptance call for:

* first verify fails -> auto-remediation -> second verify succeeds (per issue kind);
* persistent issues exhaust the round budget -> ``needs_human``;
* exhausted model budget -> ``budget_exhausted``;
* success with a degradation -> ``completed_with_degradation``;
* every report-producing path records a ``verification_round`` trace event.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict

from upgradelens.agent.loop import _run_verification_loop
from upgradelens.agent.plan import AgentPlan, PlanStatus
from upgradelens.domain.code_evidence import (
    CodeEvidenceReport,
    CodeEvidenceSummary,
    CodeUsage,
    UsageKind,
)
from upgradelens.domain.dependency import DependencyScanResult, ResolutionStatus
from upgradelens.domain.doc_evidence import DocEvidence, RetrievalRun
from upgradelens.pipeline import AssessmentRequest
from upgradelens.tools.registry import Tool, ToolContext, ToolRegistry
from upgradelens.verify.models import (
    EvidenceStatus,
    IssueCode,
    RemediationKind,
    VerificationIssue,
    VerifiedReport,
    VerifiedRisk,
    classify_issue,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _AnyArgs(BaseModel):
    model_config = ConfigDict(extra="allow")


def _stub_tool(name: str, handler) -> Tool:
    return Tool(name=name, description=name, input_model=_AnyArgs, handler=handler)


def _code_report() -> CodeEvidenceReport:
    usage = CodeUsage(
        path="app/client.py",
        start_line=1,
        end_line=1,
        column=0,
        kind=UsageKind.CALL,
        symbol="get",
        snippet="requests.get(url)",
        content_hash="deadbeef",
        is_test_code=False,
    )
    return CodeEvidenceReport(
        dependency_name="requests",
        scanned_files=1,
        usages=[usage],
        dynamic_imports=[],
        parse_errors=[],
        test_production_links=[],
        summary=CodeEvidenceSummary(
            scanned_files=1,
            usage_count=1,
            by_kind={UsageKind.CALL: 1},
        ),
    )


def _retrieval_run(run_id: str) -> RetrievalRun:
    doc = DocEvidence(
        source_id="src:pydocs",
        url="https://docs.python-requests.org/en/latest/",
        title="requests docs",
        chunk_title="Quickstart",
        snapshot_hash="snap1",
        snippet="requests.get(url)",
        score=0.9,
        matched_query="get",
    )
    return RetrievalRun(
        run_id=run_id,
        source_id="src:pydocs",
        query="requests get",
        top_doc_evidence=[doc],
    )


def _scan_result() -> DependencyScanResult:
    return DependencyScanResult(
        requested_name="requests",
        dependency_name="requests",
        status=ResolutionStatus.RESOLVED,
        current_specifier="2.31.0",
        target_version="2.32.0",
    )


def _verified_risk(issues: list[VerificationIssue], *, degraded: bool = False) -> VerifiedRisk:
    return VerifiedRisk(
        risk_id="risk-1",
        title="impact",
        status=EvidenceStatus.INSUFFICIENT_EVIDENCE if degraded else EvidenceStatus.VERIFIED,
        severity="major",
        model_severity="major",
        issues=issues,
    )


def _request(*, db: Path | None = Path("/tmp/does-not-matter.db")) -> AssessmentRequest:
    return AssessmentRequest(
        repo="/repo",
        dependency="requests",
        target_version="2.32.0",
        source_version="2.31.0",
        db=db,
        source_id="src:pydocs",
    )


def _ctx(request: AssessmentRequest, repo_path: Path) -> ToolContext:
    ctx = ToolContext()
    ctx.db = request.db
    ctx.repo_path = repo_path
    ctx.source_id = request.source_id
    return ctx


def _accumulator(repo_path: Path) -> Any:
    # ``_Accumulator`` is a module-private dataclass; construct it by name.
    from upgradelens.agent.loop import _Accumulator

    return _Accumulator(
        repo_path=repo_path,
        code_report=_code_report(),
        scan_result=_scan_result(),
        doc_runs=[],
        source_version_spec="2.31.0",
        target_version_spec="2.32.0",
    )


def _fail_then_succeed(fail_code: IssueCode, succeed_after: int):
    """analyse fake: emit ``fail_code`` for the first ``succeed_after`` calls."""
    state = {"n": 0}

    def analyse_fn(collection, gateway, ctx, registry=None):
        state["n"] += 1
        if state["n"] <= succeed_after:
            issue = VerificationIssue(
                code=fail_code, detail="missing doc evidence", evidence_id="doc:requests.get"
            )
            verified = VerifiedReport(
                verified_risks=[_verified_risk([issue])], degraded_risks=[]
            )
        else:
            verified = VerifiedReport(verified_risks=[], degraded_risks=[])
        return SimpleNamespace(verified=verified)

    analyse_fn.state = state  # type: ignore[attr-defined]
    return analyse_fn


def _registry(tool_calls: list[str]) -> ToolRegistry:
    def retrieve(args, ctx):
        tool_calls.append("retrieve_for_package")
        return [_retrieval_run(f"run-{len(tool_calls)}"), _retrieval_run(f"run2-{len(tool_calls)}")]

    def scan(args, ctx):
        tool_calls.append("scan_code")
        return _code_report()

    return ToolRegistry(
        tools=[
            _stub_tool("retrieve_for_package", retrieve),
            _stub_tool("scan_code", scan),
        ]
    )


def _plan() -> AgentPlan:
    return AgentPlan(
        request_id="r1",
        mode="fake",
        target_version_spec="2.32.0",
        source_version_spec="2.31.0",
    )


# --------------------------------------------------------------------------- #
# classify_issue unit tests
# --------------------------------------------------------------------------- #


def test_classify_issue_maps_supplement() -> None:
    assert classify_issue(IssueCode.NO_DOC_EVIDENCE) is RemediationKind.SUPPLEMENT
    assert classify_issue(IssueCode.DOC_VERSION_CONFLICT) is RemediationKind.SUPPLEMENT
    assert classify_issue(IssueCode.DOC_SOURCE_UNTRUSTED) is RemediationKind.SUPPLEMENT


def test_classify_issue_maps_rescan() -> None:
    assert classify_issue(IssueCode.CONTENT_HASH_CHANGED) is RemediationKind.RESCAN
    assert classify_issue(IssueCode.FILE_NOT_FOUND) is RemediationKind.RESCAN
    assert classify_issue(IssueCode.LINE_OUT_OF_RANGE) is RemediationKind.RESCAN


def test_classify_issue_maps_reanalyse() -> None:
    assert classify_issue(IssueCode.UNKNOWN_EVIDENCE_ID) is RemediationKind.REANALYSE
    assert classify_issue(IssueCode.NO_EVIDENCE_IDS) is RemediationKind.REANALYSE
    assert classify_issue(IssueCode.SYMBOL_NOT_IN_EVIDENCE) is RemediationKind.REANALYSE


def test_classify_issue_maps_human_for_terminal() -> None:
    assert classify_issue(IssueCode.NO_CODE_EVIDENCE) is RemediationKind.HUMAN
    assert classify_issue(IssueCode.DYNAMIC_ONLY_EVIDENCE) is RemediationKind.HUMAN
    assert classify_issue(IssueCode.UNKNOWN_TEST_PATH) is RemediationKind.HUMAN


# --------------------------------------------------------------------------- #
# verification-loop behaviour tests
# --------------------------------------------------------------------------- #


def test_supplement_issue_triggers_retrieval_then_succeeds() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    tool_calls: list[str] = []
    registry = _registry(tool_calls)
    plan = _plan()

    analyse_fn = _fail_then_succeed(IssueCode.NO_DOC_EVIDENCE, succeed_after=1)
    outcome = _run_verification_loop(
        plan, acc, request, registry, ctx, SimpleNamespace(), None,
        max_replans=3, analyse_fn=analyse_fn,
    )

    assert outcome.verified.all_risks == []  # settled
    assert plan.status == PlanStatus.COMPLETED.value
    assert plan.replan_count == 1
    assert tool_calls == ["retrieve_for_package"]
    assert len(acc.doc_runs) == 2


def test_rescan_issue_triggers_scan_code_then_succeeds() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    tool_calls: list[str] = []
    registry = _registry(tool_calls)
    plan = _plan()

    analyse_fn = _fail_then_succeed(IssueCode.CONTENT_HASH_CHANGED, succeed_after=1)
    _outcome = _run_verification_loop(
        plan, acc, request, registry, ctx, SimpleNamespace(), None,
        max_replans=3, analyse_fn=analyse_fn,
    )

    assert plan.status == PlanStatus.COMPLETED.value
    assert plan.replan_count == 1
    assert tool_calls == ["scan_code"]  # rescan, NOT retrieval


def test_reanalyse_issue_remediates_without_tool_then_succeeds() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    tool_calls: list[str] = []
    registry = _registry(tool_calls)
    plan = _plan()

    analyse_fn = _fail_then_succeed(IssueCode.UNKNOWN_EVIDENCE_ID, succeed_after=1)
    _outcome = _run_verification_loop(
        plan, acc, request, registry, ctx, SimpleNamespace(), None,
        max_replans=3, analyse_fn=analyse_fn,
    )

    assert plan.status == PlanStatus.COMPLETED.value
    assert plan.replan_count == 1
    # REANALYSE has nothing to re-collect; only the re-run analyse() runs.
    assert tool_calls == []


def test_persistent_issue_exhausts_rounds_needs_human() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    tool_calls: list[str] = []
    registry = _registry(tool_calls)
    plan = _plan()

    # never succeeds
    analyse_fn = _fail_then_succeed(IssueCode.NO_DOC_EVIDENCE, succeed_after=99)
    _outcome = _run_verification_loop(
        plan, acc, request, registry, ctx, SimpleNamespace(), None,
        max_replans=2, analyse_fn=analyse_fn,
    )

    assert plan.status == PlanStatus.NEEDS_HUMAN.value
    assert plan.replan_count == 2


def test_exhausted_budget_stops_and_marks_budget_exhausted() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    registry = _registry([])
    plan = _plan()

    gateway = SimpleNamespace(budget=SimpleNamespace(remaining_tokens=0))
    analyse_fn = _fail_then_succeed(IssueCode.NO_DOC_EVIDENCE, succeed_after=99)
    _outcome = _run_verification_loop(
        plan, acc, request, registry, ctx, gateway, None,
        max_replans=3, analyse_fn=analyse_fn,
    )

    assert plan.status == PlanStatus.BUDGET_EXHAUSTED.value
    assert plan.replan_count == 0  # no remediation happened


def test_success_with_degradation_marks_completed_with_degradation() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    registry = _registry([])
    plan = _plan()

    def analyse_fn(collection, gateway, ctx, registry=None):
        verified = VerifiedReport(
            verified_risks=[],
            degraded_risks=[_verified_risk([], degraded=True)],
            degradations=["low confidence"],
        )
        return SimpleNamespace(verified=verified)

    _run_verification_loop(
        plan, acc, request, registry, ctx, SimpleNamespace(), None,
        max_replans=3, analyse_fn=analyse_fn,
    )

    assert plan.status == PlanStatus.COMPLETED_WITH_DEGRADATION.value


def test_verification_rounds_are_recorded_in_trace() -> None:
    repo = Path("/repo")
    request = _request()
    ctx = _ctx(request, repo)
    acc = _accumulator(repo)
    tool_calls: list[str] = []
    registry = _registry(tool_calls)
    plan = _plan()

    analyse_fn = _fail_then_succeed(IssueCode.NO_DOC_EVIDENCE, succeed_after=1)
    _run_verification_loop(
        plan, acc, request, registry, ctx, SimpleNamespace(), None,
        max_replans=3, analyse_fn=analyse_fn,
    )

    rounds = [e for e in ctx.trace.events if getattr(e, "tool", None) == "verification_round"]
    # first verify failed -> one remediation -> second verify succeeded => 2 rounds
    assert len(rounds) == 2
    assert rounds[-1].params["issue_codes"] == []  # final round is clean
