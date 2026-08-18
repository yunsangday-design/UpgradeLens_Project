"""S2: Dependency Upgrade wrapped as the first concrete capability.

Offline tests: the capability's plan is deterministic, the adapters are pure model
transforms, and the analyzer node is served by pre-generated fake responses.
"""

from __future__ import annotations

from upgradelens.agent.router import route_task
from upgradelens.capabilities.dependency_upgrade import (
    DEPENDENCY_UPGRADE_STEPS,
    actions_from_impact,
    build_dependency_upgrade_capability,
    findings_from_impact,
    get_default_capabilities,
    software_task_to_request,
    verification_from_verified,
)
from upgradelens.core.action import ActionKind
from upgradelens.core.capability import CapabilityRegistry, ToolPermissionError
from upgradelens.core.finding import FindingStatus
from upgradelens.llm.fixtures_core import build_fake_core_responses
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import ImpactReport, RiskItem
from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    VerifiedReport,
    VerifiedRisk,
)


def _fake_gateway() -> ModelGateway:
    return ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )


def test_default_capabilities_registers_dependency_upgrade() -> None:
    caps = get_default_capabilities()
    assert any(c.kind == "dependency_upgrade" for c in caps)
    reg = CapabilityRegistry()
    for cap in caps:
        reg.register(cap)
    assert reg.get("dependency_upgrade") is not None
    # The capability may call the upgrade pipeline tools.
    reg.require_tool("dependency_upgrade", "scan_code")
    # A tool outside the declared set is rejected.
    try:
        reg.require_tool("dependency_upgrade", "verify_report")
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError for undeclared tool")


def test_capability_build_plan_returns_deterministic_steps() -> None:
    cap = build_dependency_upgrade_capability()
    task = route_task("upgrade pydantic in https://github.com/foo/bar to 2.0.0", task_id="s2")
    plan = cap.build_plan(task)
    assert plan.capability_kind == "dependency_upgrade"
    assert tuple(plan.steps) == DEPENDENCY_UPGRADE_STEPS
    assert set(cap.allowed_tools) >= set(DEPENDENCY_UPGRADE_STEPS)
    assert cap.coverage_policy.forbidden_auto_fix is True
    assert "target_version" in cap.coverage_policy.required_inputs


def test_adapters_findings_and_actions() -> None:
    report = ImpactReport(
        target_dependency="pydantic",
        source_version_spec="1.10",
        target_version_spec="2.0",
        risks=[
            RiskItem(
                risk_id="r1",
                title=".dict() removed",
                severity="high",
                confidence="high",
                evidence_ids=["code-1"],
                recommendation="Use model_dump() instead of .dict().",
            )
        ],
        evidence_summary={"code_usage": 1},
    )
    findings = findings_from_impact(report)
    assert len(findings) == 1
    assert findings[0].finding_id == "r1"
    assert findings[0].status == FindingStatus.VERIFIED
    assert findings[0].evidence_ids == ["code-1"]

    actions = actions_from_impact(report)
    assert len(actions) == 1
    assert actions[0].kind == ActionKind.MANUAL
    assert actions[0].requires_approval is True
    assert actions[0].finding_ids == ["r1"]


def test_adapter_verification_from_verified() -> None:
    base = dict(
        target_dependency="pydantic",
        target_version_spec="2.0",
        conclusion=Conclusion.IMPACTED,
        verified_risks=[
            VerifiedRisk(
                risk_id="r1",
                title=".dict() removed",
                status=EvidenceStatus.VERIFIED,
                severity="high",
                model_severity="high",
            )
        ],
    )
    verified = VerifiedReport(**base)
    result = verification_from_verified(verified)
    assert result.passed is True
    assert result.summary == "conclusion=impacted"
    assert result.checks[0].passed is True

    # A degraded risk flips the aggregate to failed.
    degraded = VerifiedReport(
        **base,
        degraded_risks=[
            VerifiedRisk(
                risk_id="r2",
                title="unknown use",
                status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
                severity="medium",
                model_severity="medium",
            )
        ],
    )
    assert verification_from_verified(degraded).passed is False


def test_software_task_to_request() -> None:
    task = route_task("upgrade pydantic in https://github.com/foo/bar to 2.0.0", task_id="s2")
    request = software_task_to_request(task)
    assert request.repo == "https://github.com/foo/bar"
    assert request.dependency == "pydantic"
    assert request.target_version == "2.0.0"


def test_fake_end_to_end_capability_flow() -> None:
    gw = _fake_gateway()
    task = route_task("upgrade pydantic in https://github.com/foo/bar to 2.0.0", task_id="s2")
    cap = build_dependency_upgrade_capability()
    plan = cap.build_plan(task)
    assert plan.steps  # deterministic steps exist

    # Analyzer node is served entirely by the pre-generated fake response.
    report, used = gw.complete_structured(prompt="analyze", schema=ImpactReport, name="analyse")
    assert isinstance(report, ImpactReport)
    assert used.mode == "fake"
    findings = findings_from_impact(report)
    assert findings and findings[0].status == FindingStatus.VERIFIED
