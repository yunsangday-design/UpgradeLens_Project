"""S7 integration tests for the Security Review capability (offline, fake mode).

The model node ``security_review`` is served from ``build_fake_core_responses``; every
other step reuses the deterministic change/repository/semgrep packages. No network and
no real model call occurs -- this is exactly the doc's offline acceptance for S7.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from upgradelens.capabilities.defaults import (
    default_capability_registry,
    get_default_capabilities,
)
from upgradelens.capabilities.security_review import (
    compute_security_coverage,
    get_security_review_capability,
    review_security,
    security_review_tools,
    security_review_verifier,
)
from upgradelens.capabilities.security_review.capability import build_security_review_plan
from upgradelens.capabilities.security_review.tools import SECURITY_REVIEW_TOOL_NAMES
from upgradelens.change.diff import parse_unified_diff
from upgradelens.core.capability import CapabilityRegistry, ToolPermissionError
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.security import CWE, SecurityCategory
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.integrations.semgrep import SemgrepResult, run_semgrep, to_sarif
from upgradelens.integrations.semgrep.adapter import _scan_text
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

SECRET_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def handler():
-    return cfg.value
+    return cfg.value if cfg else DEFAULT
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


def test_review_security_offline(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    gw = _fake_gateway()
    result = review_security(
        repo_root=tmp_path, unified_diff=APP_DIFF, gateway=gw
    )

    # Model node served from the canned fixture, not a real call.
    assert result.used_model is False
    assert result.report.review_id == "sec-001"
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.category == f"security:{SecurityCategory.SECRET.value}"
    assert finding.severity == Severity.HIGH
    assert finding.status == FindingStatus.VERIFIED
    assert finding.evidence_ids == ["code:src/app.py:42"]

    # Verified finding cites real changed code -> the security gate passes.
    assert result.gate.passed is True

    # Coverage: src/app.py is cited by the verified finding.
    coverage = compute_security_coverage(result.findings, result.change_set)
    assert coverage.changed_files == 1
    assert coverage.cited_files == 1
    assert coverage.coverage == 1.0

    # Repository profile was built statically.
    assert "py" in result.profile.languages or result.profile.languages


def test_security_review_plan_deterministic() -> None:
    cap = get_security_review_capability()
    task = SoftwareTask(
        task_id="t",
        kind=TaskKind.SECURITY_REVIEW,
        goal="",
        context=TaskContext(repo="."),
    )
    plan = cap.build_plan(task)
    assert plan.capability_kind == "security_review"
    assert plan.steps == list(SECURITY_REVIEW_TOOL_NAMES)
    assert build_security_review_plan(task).capability_kind == "security_review"


def test_security_verifier_blocks_high_unverified() -> None:
    change_set = parse_unified_diff(APP_DIFF)
    bad = Finding(
        finding_id="x1",
        category="security:secret",
        severity=Severity.HIGH,
        summary="suspect call",
        status=FindingStatus.VERIFIED,
        evidence_ids=["code:does_not_exist.py:1"],
    )
    result = security_review_verifier([bad], change_set)
    assert result.passed is False
    assert result.checks[0].passed is False


def test_security_verifier_passes_false_positive() -> None:
    change_set = parse_unified_diff(APP_DIFF)
    fp = Finding(
        finding_id="x2",
        category="security:secret",
        severity=Severity.CRITICAL,
        summary="accidental secret",
        status=FindingStatus.REJECTED,
        evidence_ids=[],
    )
    result = security_review_verifier([fp], change_set)
    assert result.passed is True


def test_security_review_tool_gating() -> None:
    cap_reg = CapabilityRegistry()
    cap_reg.register(get_security_review_capability())
    reg = ToolRegistry(security_review_tools(), capability_registry=cap_reg)
    reg.set_active_capability("security_review")

    for tool in SECURITY_REVIEW_TOOL_NAMES:
        cap_reg.require_tool("security_review", tool)

    out = reg.run("load_change_set", {"unified_diff": APP_DIFF}, None)
    assert out["files_changed"] == 1

    try:
        reg.run("delete_production_db", {}, None)
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("expected ToolPermissionError for undeclared tool")


def test_dependency_cve_check_finds_known_cve(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django==2.2.0\n", encoding="utf-8")
    from upgradelens.capabilities.security_review.analyzers import (
        check_dependency_cves,
    )

    findings = check_dependency_cves(tmp_path, "django", None)
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH
    assert findings[0].cwe == CWE.CWE_937
    assert findings[0].category == SecurityCategory.DEPENDENCY


def test_capability_registered() -> None:
    kinds = {getattr(c.kind, "value", c.kind) for c in get_default_capabilities()}
    assert "security_review" in kinds
    catalog = {c["kind"] for c in default_capability_registry().catalog()}
    assert "security_review" in catalog


# --- S7 completion criteria: true positive / false positive / suppressed / fixed ---


def test_fake_scan_true_positive() -> None:
    code = 'API_KEY = "abcdefghijklmnop"\n'
    findings = _scan_text(code, "app/main.py")
    assert len(findings) == 1
    assert findings[0].category == SecurityCategory.SECRET
    assert findings[0].false_positive is False
    assert findings[0].status == FindingStatus.CANDIDATE
    assert findings[0].evidence_refs[0].startswith("code:")


def test_fake_scan_suppresses_allowlisted_path() -> None:
    # Findings in tests/fixtures/examples are suppressed (false-positive path).
    code = 'API_KEY = "abcdefghijklmnop"\n'
    assert _scan_text(code, "tests/secret_test.py") == []
    assert _scan_text(code, "fixtures/sample.py") == []
    assert _scan_text(code, "examples/demo.py") == []


def test_fixed_then_gone() -> None:
    vulnerable = 'password = "abcdefghijklmnop"'
    fixed = "password = get_secret()"
    before = _scan_text(vulnerable, "app/config.py")
    after = _scan_text(fixed, "app/config.py")
    assert len(before) == 1
    # Re-running the same rule after the fix must yield nothing.
    assert after == []


def test_scanner_finding_without_location_cannot_be_verified() -> None:
    # A scanner finding with no code location can never pass the gate: it stays
    # CANDIDATE (never auto-VERIFIED) and a HIGH candidate with no cited changed
    # code fails the gate. (The Finding model also forbids VERIFIED without
    # evidence, so the pipeline can never mark it verified.)
    change_set = parse_unified_diff(APP_DIFF)
    no_loc = Finding(
        finding_id="scan-no-loc",
        category="security:secret",
        severity=Severity.HIGH,
        summary="scanner hit with no code location",
        status=FindingStatus.CANDIDATE,
        evidence_ids=[],
    )
    result = security_review_verifier([no_loc], change_set)
    assert result.passed is False


def test_finding_model_forbids_verified_without_evidence() -> None:
    # Hard guarantee behind "scanner finding without a location can't be verified".
    with pytest.raises(ValidationError):
        Finding(
            finding_id="x",
            category="security:secret",
            severity=Severity.HIGH,
            status=FindingStatus.VERIFIED,
            evidence_ids=[],
        )


def test_sarif_projection() -> None:
    code = 'TOKEN = "abcdefghijklmnop"\n'
    findings = _scan_text(code, "app/main.py")
    sarif = to_sarif(SemgrepResult(findings=findings, used_fake=True))
    assert sarif["version"] == "2.1.0"
    assert (
        sarif["runs"][0]["tool"]["driver"]["name"] == "upgradelens-security-review"
    )
    assert len(sarif["runs"][0]["results"]) == len(findings)
    assert sarif["runs"][0]["results"][0]["ruleId"].startswith("semgrep:")


def test_run_semgrep_offline_returns_result(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        'SECRET = "abcdefghijklmnop"\n', encoding="utf-8"
    )
    res = run_semgrep(tmp_path, fake=True)
    assert res.used_fake is True
    assert any(f.category == SecurityCategory.SECRET for f in res.findings)


def test_run_semgrep_rejects_disallowed_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_semgrep(tmp_path, fake=False, config="evil-ruleset")
