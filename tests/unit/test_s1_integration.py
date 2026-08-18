"""S1 integration: wire SoftwareTask + CapabilityRegistry into the existing agents.

These tests run fully offline -- the router and planner use ``fake`` mode (rule
extraction / deterministic plan), the LLM-dependent analyzer node is served by
pre-generated canned responses, and no network or real model is touched.
"""

from __future__ import annotations

from upgradelens.agent.planner import build_agent_plan
from upgradelens.agent.router import route_task
from upgradelens.agent.run_store import RunStore
from upgradelens.core.action import ActionKind, ActionProposal
from upgradelens.core.capability import (
    BaseCapability,
    CapabilityPlan,
    CapabilityRegistry,
    ToolPermissionError,
)
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.task import SoftwareTask, TaskKind
from upgradelens.core.verification import VerificationCheck, VerificationResult
from upgradelens.llm.fixtures_core import build_fake_core_responses
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import ImpactReport
from upgradelens.tools.registry import ToolRegistry


def _fake_gateway() -> ModelGateway:
    return ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )


def _registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()

    class DependencyUpgradeCap(BaseCapability):
        def build_plan(self, task: SoftwareTask) -> CapabilityPlan:
            return CapabilityPlan(
                task_id=task.task_id,
                capability_kind=self.kind,
                steps=["clone_repo", "scan_code", "retrieve_docs", "verify_report"],
                note="dependency upgrade plan",
            )

    class PRReviewCap(BaseCapability):
        def build_plan(self, task: SoftwareTask) -> CapabilityPlan:
            return CapabilityPlan(
                task_id=task.task_id,
                capability_kind=self.kind,
                steps=["scan_code", "comment_pr"],
                note="pr review plan",
            )

    reg.register(
        DependencyUpgradeCap(
            kind="dependency_upgrade",
            name="Dependency Upgrade",
            allowed_tools=["scan_code", "retrieve_docs", "verify_report", "clone_repo"],
        )
    )
    reg.register(
        PRReviewCap(
            kind="pr_review",
            name="PR Review",
            allowed_tools=["scan_code"],
        )
    )
    return reg


def test_route_task_wraps_intent_offline() -> None:
    task = route_task(
        "upgrade pydantic in https://github.com/foo/bar to 2.0.0",
        task_id="t1",
    )
    assert isinstance(task, SoftwareTask)
    assert task.task_id == "t1"
    assert task.kind == TaskKind.DEPENDENCY_UPGRADE
    assert task.context.repo == "https://github.com/foo/bar"
    assert task.context.dependency == "pydantic"
    assert task.context.target_version == "2.0.0"


def test_planner_delegates_to_capability() -> None:
    reg = _registry()
    task = route_task(
        "upgrade pydantic in https://github.com/foo/bar to 2.0.0",
        task_id="t2",
    )
    plan = build_agent_plan(
        gateway=_fake_gateway(),
        registry=ToolRegistry(),
        repo=task.context.repo,
        dependency=task.context.dependency,
        target_version=task.context.target_version,
        task=task,
        capability_registry=reg,
    )
    # Steps come from the dependency_upgrade capability, not DEFAULT_PLAN_STEPS.
    assert [s.tool for s in plan.steps] == [
        "clone_repo",
        "scan_code",
        "retrieve_docs",
        "verify_report",
    ]
    assert all(s.phase == "dependency_upgrade" for s in plan.steps)


def test_planner_falls_back_when_no_capability() -> None:
    task = route_task(
        "upgrade pydantic in https://github.com/foo/bar to 2.0.0",
        task_id="t3",
    )
    plan = build_agent_plan(
        gateway=_fake_gateway(),
        registry=ToolRegistry(),
        repo=task.context.repo,
        dependency=task.context.dependency,
        target_version=task.context.target_version,
        task=task,
        # No registry -> deterministic default plan.
        capability_registry=None,
    )
    assert [s.tool for s in plan.steps][0] == "clone_repo"


def test_capability_tool_permission_gate() -> None:
    reg = _registry()
    # dependency_upgrade is allowed to call verify_report.
    reg.require_tool("dependency_upgrade", "verify_report")
    # pr_review is NOT allowed to call verify_report -> must raise.
    try:
        reg.require_tool("pr_review", "verify_report")
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError for pr_review/verify_report")
    # pr_review may call scan_code.
    reg.require_tool("pr_review", "scan_code")


def test_tool_registry_enforces_active_capability() -> None:
    reg = _registry()
    tool_reg = ToolRegistry(capability_registry=reg)
    # No active capability -> no gating.
    tool_reg._enforce_capability("verify_report")
    # Activate pr_review -> verify_report must be rejected.
    tool_reg.set_active_capability("pr_review")
    try:
        tool_reg._enforce_capability("verify_report")
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError from active capability gate")
    tool_reg._enforce_capability("scan_code")
    # Deactivate -> gating off again.
    tool_reg.set_active_capability(None)
    tool_reg._enforce_capability("verify_report")


def test_fake_gateway_serves_canned_analyzer_response() -> None:
    gw = _fake_gateway()
    report, used = gw.complete_structured(prompt="x", schema=ImpactReport, name="analyse")
    assert isinstance(report, ImpactReport)
    # Fake mode never calls a real model -- the response was served from the
    # pre-generated fixture, not a network request.
    assert used.mode == "fake"


def test_run_store_persists_core_models(tmp_path) -> None:
    store = RunStore.create(tmp_path, "run-s1")

    task = route_task(
        "upgrade pydantic in https://github.com/foo/bar to 2.0.0",
        task_id="t4",
    )
    store.write_software_task(task)
    assert store.read_software_task() is not None
    assert store.read_software_task().task_id == "t4"

    findings = [
        Finding(
            finding_id="f1",
            category="breaking_change",
            severity=Severity.HIGH,
            summary="X was removed",
            detail="X removed in 2.0",
            status=FindingStatus.VERIFIED,
            evidence_ids=["ev1"],
            confidence=0.9,
        )
    ]
    store.write_findings(findings)
    read_findings = store.read_findings()
    assert read_findings[0].finding_id == findings[0].finding_id
    assert read_findings[0].status == FindingStatus.VERIFIED

    actions = [
        ActionProposal(
            proposal_id="a1",
            kind=ActionKind.PATCH,
            finding_ids=[findings[0].finding_id],
            title="Pin X",
            requires_approval=True,
        )
    ]
    store.write_actions(actions)
    assert store.read_actions()[0].proposal_id == "a1"

    verification = VerificationResult(
        proposal_id="a1",
        checks=[VerificationCheck(name="c1", passed=True, detail="basic check")],
        summary="ok",
    )
    store.write_verification(verification)
    assert store.read_verification() is not None
    assert store.read_verification().passed is True
