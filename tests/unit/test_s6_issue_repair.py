"""S6 integration tests for the Issue Repair capability (offline, fake mode)."""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities.issue_repair import (
    get_issue_repair_capability,
    issue_repair_tools,
    render_issue_repair,
    repair_issue,
    verify_issue_repair,
)
from upgradelens.capabilities.issue_repair.models import IssueRepairReport
from upgradelens.capabilities.issue_repair.tools import ISSUE_REPAIR_TOOL_NAMES
from upgradelens.core.action import PatchProposal
from upgradelens.core.capability import CapabilityRegistry, ToolPermissionError
from upgradelens.core.finding import FindingStatus
from upgradelens.llm.fixtures_core import build_fake_core_responses
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.tools.registry import ToolRegistry

ISSUE_TEXT = """\
# ISSUE-42: App crashes when config missing

The handler dereferences cfg.value but cfg can be None when the config
file is absent. Steps to reproduce: start without config.
"""


def _make_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(
        "def handle(cfg):\n    return cfg.value\n", encoding="utf-8"
    )


def _fake_gateway() -> ModelGateway:
    return ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )


def test_repair_issue_offline(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = repair_issue(repo_root=tmp_path, issue_text=ISSUE_TEXT, gateway=gw)

    assert result.used.mode == "fake"
    assert result.report.report_id == "rep-001"
    assert result.issue.issue_id == "ISSUE-42"
    assert result.actions  # a patch was proposed
    assert result.actions[0].target_files == ["src/app.py"]
    # The patch targets a real file -> verification passes.
    assert result.verification.passed is True
    assert result.findings[0].status == FindingStatus.VERIFIED


def test_issue_repair_renderer(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = repair_issue(repo_root=tmp_path, issue_text=ISSUE_TEXT, gateway=gw)
    text = render_issue_repair(result)
    assert "Issue Repair" in text
    assert "src/app.py" in text
    assert "PASS" in text


def test_issue_repair_tool_gating() -> None:
    cap_reg = CapabilityRegistry()
    cap_reg.register(get_issue_repair_capability())
    reg = ToolRegistry(issue_repair_tools(), capability_registry=cap_reg)
    reg.set_active_capability("issue_repair")

    for tool in ISSUE_REPAIR_TOOL_NAMES:
        cap_reg.require_tool("issue_repair", tool)

    out = reg.run("load_issue", {"issue_text": ISSUE_TEXT}, None)
    assert out["issue_id"] == "ISSUE-42"

    try:
        reg.run("sudo_rewrite_disk", {}, None)
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError for undeclared tool")


def test_verifier_rejects_missing_target() -> None:
    report = IssueRepairReport(
        report_id="rep-x",
        patch=PatchProposal(proposal_id="p", target_files=["does_not_exist.py"]),
    )
    result = verify_issue_repair(report, Path("/nonexistent_repo_xyz"))
    assert result.passed is False
