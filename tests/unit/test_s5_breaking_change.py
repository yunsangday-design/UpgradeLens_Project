"""S5 integration tests for the Breaking Change capability (offline, fake mode)."""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities.breaking_change import (
    breaking_change_tools,
    get_breaking_change_capability,
    render_breaking_change,
    review_breaking_changes,
    verify_breaking_changes,
)
from upgradelens.capabilities.breaking_change.models import ApiChangeKind
from upgradelens.capabilities.breaking_change.tools import BREAKING_CHANGE_TOOL_NAMES
from upgradelens.change.diff import parse_unified_diff
from upgradelens.core.capability import CapabilityRegistry, ToolPermissionError
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.llm.fixtures_core import build_fake_core_responses
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.tools.registry import ToolRegistry

APP_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,4 @@
 def handler():
     return 1
+
+def new_feature():
+    return 2
"""


def _make_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(
        "def handler():\n    return 1\n\n\ndef new_feature():\n    return 2\n",
        encoding="utf-8",
    )


def _fake_gateway() -> ModelGateway:
    return ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )


def test_review_breaking_changes_offline(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = review_breaking_changes(
        repo_root=tmp_path,
        unified_diff=APP_DIFF,
        from_version="1.10",
        to_version="2.0",
        gateway=gw,
    )

    assert result.used.mode == "fake"
    assert result.report.report_id == "bc-001"
    assert result.comparison.level == "major"
    assert len(result.findings) == 2

    by_id = {f.finding_id: f for f in result.findings}
    assert by_id["bc:model_dump"].category == "breaking_change"
    assert by_id["bc:model_dump"].severity == Severity.HIGH
    assert by_id["bc:model_dump"].status == FindingStatus.VERIFIED
    assert by_id["bc:old_helper"].severity == Severity.CRITICAL

    assert result.verification.passed is True
    assert "code:src/app.py:12" in result.verification.evidence_ids


def test_breaking_change_renderer(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = review_breaking_changes(
        repo_root=tmp_path,
        unified_diff=APP_DIFF,
        from_version="1.10",
        to_version="2.0",
        gateway=gw,
    )
    text = render_breaking_change(result)
    assert "Breaking Changes" in text
    assert "major" in text
    assert ApiChangeKind.SIGNATURE_CHANGE.value in text


def test_breaking_change_tool_gating() -> None:
    cap_reg = CapabilityRegistry()
    cap_reg.register(get_breaking_change_capability())
    reg = ToolRegistry(breaking_change_tools(), capability_registry=cap_reg)
    reg.set_active_capability("breaking_change")

    for tool in BREAKING_CHANGE_TOOL_NAMES:
        cap_reg.require_tool("breaking_change", tool)

    out = reg.run("load_change_set", {"unified_diff": APP_DIFF}, None)
    assert out["files_changed"] == 1

    try:
        reg.run("drop_database", {}, None)
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError for undeclared tool")


def test_verifier_rejects_unverifiable_breaking_change() -> None:
    change_set = parse_unified_diff(APP_DIFF)
    bad = Finding(
        finding_id="bc:x",
        category="breaking_change",
        severity=Severity.HIGH,
        summary="suspect break",
        status=FindingStatus.VERIFIED,
        evidence_ids=["code:nope.py:1"],
    )
    result = verify_breaking_changes([bad], change_set)
    assert result.passed is False
    assert result.checks[0].passed is False
