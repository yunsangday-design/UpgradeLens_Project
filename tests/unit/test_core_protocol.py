"""Deterministic tests for the generic task runtime contracts (plan stage S1).

None of these tests call a model. They pin the structural guarantees the runtime
relies on:

- a ``VERIFIED`` finding must cite evidence (anti-hallucination);
- every action proposal defaults to requiring approval;
- a capability may only call tools it declared;
- two capabilities produce distinct plan templates.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from upgradelens.core.action import (
    ActionKind,
    ActionProposal,
    CommandProposal,
    ManualAction,
    PatchProposal,
    TestProposal,
)
from upgradelens.core.capability import (
    BaseCapability,
    CapabilityPlan,
    CapabilityRegistry,
    ToolPermissionError,
)
from upgradelens.core.finding import EvidenceLink, Finding, FindingStatus, Severity
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.core.verification import VerificationCheck, VerificationResult

# -- task -------------------------------------------------------------------- #


def test_task_defaults_to_unknown_kind() -> None:
    task = SoftwareTask(task_id="t1")
    assert task.kind is TaskKind.UNKNOWN
    assert task.context.repo == ""


def test_task_context_extra_keys_allowed() -> None:
    task = SoftwareTask(
        task_id="t2",
        kind=TaskKind.PR_REVIEW,
        context=TaskContext(repo="./x", pr_number=12),
    )
    assert task.model_dump_context()["pr_number"] == 12


def test_task_is_frozen() -> None:
    task = SoftwareTask(task_id="t3")
    with pytest.raises(Exception):  # noqa: B017  # pydantic FrozenInstanceError
        task.kind = TaskKind.SECURITY_REVIEW  # type: ignore[misc]


# -- finding ----------------------------------------------------------------- #


def test_verified_finding_requires_evidence() -> None:
    with pytest.raises(ValueError, match="VERIFIED"):
        Finding(
            finding_id="f1",
            category="dependency",
            status=FindingStatus.VERIFIED,
            summary="must cite evidence",
        )


def test_candidate_finding_without_evidence_ok() -> None:
    f = Finding(
        finding_id="f2",
        category="dependency",
        status=FindingStatus.CANDIDATE,
        summary="hypothesis, no evidence yet",
    )
    assert f.status is FindingStatus.CANDIDATE
    assert f.evidence_ids == []


def test_verified_finding_with_evidence_ok() -> None:
    f = Finding(
        finding_id="f3",
        category="dependency",
        status=FindingStatus.VERIFIED,
        severity=Severity.HIGH,
        confidence=0.9,
        summary="pydantic v1 BaseSettings removed",
        evidence_ids=["doc:pydantic:42", "code:pydantic:7"],
    )
    assert f.status is FindingStatus.VERIFIED


def test_finding_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="confidence"):
        Finding(
            finding_id="f4",
            category="pr",
            confidence=1.7,
            summary="bad confidence",
        )


def test_finding_empty_summary_rejected() -> None:
    with pytest.raises(ValueError, match="summary"):
        Finding(finding_id="f5", category="pr", summary="   ")


def test_evidence_link_carries_kind() -> None:
    link = EvidenceLink(evidence_id="doc:1", kind="doc_chunk", summary="notes")
    assert link.kind == "doc_chunk"


# -- action ------------------------------------------------------------------ #


def test_action_default_requires_approval() -> None:
    a = ActionProposal(proposal_id="a1", kind=ActionKind.MANUAL)
    assert a.requires_approval is True


def test_patch_proposal_is_patch_kind() -> None:
    p = PatchProposal(proposal_id="p1", diff="--- a\n+++ b\n", target_files=["x.py"])
    assert p.kind is ActionKind.PATCH
    assert p.requires_approval is True
    assert p.target_files == ["x.py"]


def test_test_proposal_marks_repro() -> None:
    t = TestProposal(
        proposal_id="t1",
        test_paths=["tests/test_x.py"],
        intended_to_fail_before_fix=True,
    )
    assert t.kind is ActionKind.TEST
    assert t.intended_to_fail_before_fix is True


def test_command_proposal_not_auto_allowed() -> None:
    c = CommandProposal(proposal_id="c1", command="pytest")
    assert c.kind is ActionKind.COMMAND
    assert c.allowed is False


def test_manual_action_requires_approval() -> None:
    m = ManualAction(proposal_id="m1", instructions="bump version")
    assert m.kind is ActionKind.MANUAL
    assert m.requires_approval is True


# -- verification ------------------------------------------------------------- #


def test_verification_passed_requires_all_checks() -> None:
    res = VerificationResult(
        proposal_id="p1",
        checks=[
            VerificationCheck(name="sandbox-apply", passed=True),
            VerificationCheck(name="tests", passed=True),
        ],
    )
    assert res.passed is True


def test_verification_fails_on_any_failed_check() -> None:
    res = VerificationResult(
        proposal_id="p1",
        checks=[
            VerificationCheck(name="sandbox-apply", passed=True),
            VerificationCheck(name="tests", passed=False, detail="2 failed"),
        ],
    )
    assert res.passed is False


def test_verification_no_checks_is_not_passed() -> None:
    res = VerificationResult(proposal_id="p1")
    assert res.passed is False


# -- capability registry ----------------------------------------------------- #


@dataclass
class _FakeCapability:
    """Minimal capability for tests (Protocol is structural, not inherited)."""

    kind: str = "fake_pr"
    name: str = "Fake PR Review"
    description: str = "test double"
    allowed_tools: tuple[str, ...] = ("load_change_set", "analyze_change_impact")

    def build_plan(self, task: SoftwareTask) -> CapabilityPlan:
        return CapabilityPlan(
            task_id=task.task_id,
            capability_kind=self.kind,
            steps=[f"step_for_{self.kind}_1", f"step_for_{self.kind}_2"],
        )

    def extra_verifier_names(self) -> tuple[str, ...]:
        return ("pr_verifier",)


def test_registry_register_and_get() -> None:
    reg = CapabilityRegistry()
    cap = _FakeCapability()
    reg.register(cap)
    assert reg.get("fake_pr") is cap
    assert reg.get("missing") is None


def test_registry_rejects_empty_kind() -> None:
    reg = CapabilityRegistry()
    with pytest.raises(ValueError, match="kind"):
        reg.register(_FakeCapability(kind=""))


def test_allow_tool_enforces_declared_set() -> None:
    reg = CapabilityRegistry()
    reg.register(_FakeCapability())
    assert reg.allow_tool("fake_pr", "load_change_set") is True
    assert reg.allow_tool("fake_pr", "delete_repo") is False


def test_require_tool_raises_for_undeclared() -> None:
    reg = CapabilityRegistry()
    reg.register(_FakeCapability())
    reg.require_tool("fake_pr", "load_change_set")
    with pytest.raises(ToolPermissionError):
        reg.require_tool("fake_pr", "delete_repo")


def test_two_capabilities_produce_distinct_plans() -> None:
    reg = CapabilityRegistry()
    reg.register(_FakeCapability(kind="pr", allowed_tools=("a", "b")))
    reg.register(_FakeCapability(kind="issue", allowed_tools=("c", "d")))
    plan_pr = reg.get("pr").build_plan(SoftwareTask(task_id="t"))
    plan_issue = reg.get("issue").build_plan(SoftwareTask(task_id="t"))
    assert plan_pr.steps != plan_issue.steps
    assert plan_pr.capability_kind == "pr"


def test_catalog_exposes_allowed_tools() -> None:
    reg = CapabilityRegistry()
    reg.register(_FakeCapability())
    cat = reg.catalog()
    assert cat[0]["allowed_tools"] == ["load_change_set", "analyze_change_impact"]


def test_base_capability_builds_template_from_tools() -> None:
    cap = BaseCapability(kind="x", allowed_tools=("t1", "t2"))
    plan = cap.build_plan(SoftwareTask(task_id="t"))
    assert plan.steps == ["t1", "t2"]
    assert cap.extra_verifier_names() == ()
