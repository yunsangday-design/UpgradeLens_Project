"""S4 integration tests for the PR Review capability (offline, fake mode).

The model node ``pr_review`` is served from ``build_fake_core_responses``; every
other step reuses the deterministic S3 change/repository packages. No network and
no real model call occurs.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities.pr_review import (
    compute_pr_review_coverage,
    get_pr_review_capability,
    pr_review_tools,
    pr_review_verifier,
    render_pr_review,
    review_pull_request,
)
from upgradelens.capabilities.pr_review.models import ReviewCategory
from upgradelens.capabilities.pr_review.tools import PR_REVIEW_TOOL_NAMES
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
    (root / "README.md").write_text("# demo\n", encoding="utf-8")


def _fake_gateway() -> ModelGateway:
    return ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )


def test_review_pull_request_offline(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = review_pull_request(repo_root=tmp_path, unified_diff=APP_DIFF, gateway=gw)

    # Model node served from the canned fixture, not a real call.
    assert result.used.mode == "fake"
    assert result.report.review_id == "rev-001"
    assert len(result.findings) == 3

    by_id = {f.finding_id: f for f in result.findings}
    assert by_id["c1"].category == ReviewCategory.LOGIC_RISK.value
    assert by_id["c1"].severity == Severity.HIGH
    assert by_id["c1"].status == FindingStatus.VERIFIED
    assert by_id["c1"].evidence_ids == ["code:src/app.py:12"]
    # The test-gap finding must stay a candidate, not be auto-verified.
    assert by_id["c2"].status == FindingStatus.CANDIDATE

    # Verified findings cite real changed code -> verification passes.
    assert result.verification.passed is True
    assert "code:src/app.py:12" in result.verification.evidence_ids

    # Coverage: src/app.py is cited by the verified findings.
    coverage = compute_pr_review_coverage(result.findings, result.change_set)
    assert coverage.changed_files == 1
    assert coverage.cited_files == 1
    assert coverage.coverage == 1.0

    # Test intelligence recommends (or proposes) tests for the changed file.
    assert result.tests
    assert any("test_app.py" in p for t in result.tests for p in t.test_paths)


def test_pr_review_renderer(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = review_pull_request(repo_root=tmp_path, unified_diff=APP_DIFF, gateway=gw)
    text = render_pr_review(result)
    assert "PR Review" in text
    assert "logic_risk" in text
    assert "PASS" in text


def test_pr_review_tool_gating() -> None:
    cap_reg = CapabilityRegistry()
    cap_reg.register(get_pr_review_capability())
    reg = ToolRegistry(pr_review_tools(), capability_registry=cap_reg)
    reg.set_active_capability("pr_review")

    # All declared tools are permitted (via the capability registry gate).
    for tool in PR_REVIEW_TOOL_NAMES:
        cap_reg.require_tool("pr_review", tool)

    # The gate is enforced at run time through the tool registry too.
    out = reg.run("load_change_set", {"unified_diff": APP_DIFF}, None)
    assert out["files_changed"] == 1

    # A tool outside the declared set is rejected.
    try:
        reg.run("delete_production_db", {}, None)
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError for undeclared tool")


def test_verifier_rejects_unverifiable_finding() -> None:
    change_set = parse_unified_diff(APP_DIFF)
    bad = Finding(
        finding_id="x1",
        category="security",
        severity=Severity.HIGH,
        summary="suspect call",
        status=FindingStatus.VERIFIED,
        evidence_ids=["code:does_not_exist.py:1"],
    )
    result = pr_review_verifier([bad], change_set)
    assert result.passed is False
    assert result.checks[0].passed is False
