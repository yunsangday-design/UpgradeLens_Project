"""Offline (fake-mode) tests for the M4 MCP capability tools.

Exercises ``list_unified_capabilities`` / ``run_capability`` / ``run_supervisor``
without any network or API key. A real LLM (live mode) is NOT exercised here;
those paths are deferred to live verification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mcp")

from upgradelens.mcp import server as mcp_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = ROOT / "tests/fixtures/eval/pydantic_field_validator/repo"


def test_list_unified_capabilities():
    out = mcp_server.list_unified_capabilities()
    assert "capabilities" in out
    kinds = {c["kind"] for c in out["capabilities"]}
    assert {
        "dependency_upgrade",
        "pr_review",
        "issue_repair",
        "security_review",
        "breaking_change",
    }.issubset(kinds)


def test_run_capability_fake():
    result = mcp_server.run_capability(
        "security_review", repo=str(FIXTURE_REPO), mode="fake"
    )
    assert result["capability"] == "security_review"
    assert "status" in result


def test_run_capability_unknown_kind():
    out = mcp_server.run_capability("not_a_capability", mode="fake")
    assert "error" in out
    assert "known_kinds" in out


def test_run_supervisor_fake_single():
    out = mcp_server.run_supervisor("fix bug: login button fails", mode="fake")
    assert out["orchestration"] == "single"
    assert out["capability_kinds"] == ["issue_repair"]
    assert out["result"] is not None


def test_run_supervisor_fake_multi():
    out = mcp_server.run_supervisor(
        "review this PR and run a security scan",
        repo=str(FIXTURE_REPO),
        mode="fake",
    )
    assert out["orchestration"] == "multi-agent"
    assert set(out["capability_kinds"]) == {"pr_review", "security_review"}
    assert len(out["sub_results"]) == 2
