"""Offline (fake-mode) tests for the A4 unified entry: CLI ``run`` + MCP ``run_task``.

Exercises argument parsing and one fake end-to-end run per surface without any
network or API key. Live paths are deferred to live verification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upgradelens.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = ROOT / "tests/fixtures/eval/pydantic_field_validator/repo"


def _run_cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli_main(["run", *argv])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_cli_run_fake_issue_repair(capsys):
    code, out = _run_cli(capsys, "fix bug: login button fails")
    assert code == 0
    assert out["capabilities"] == ["issue_repair"]
    assert out["error"] is None
    assert out["result"] is not None


def test_cli_run_dry_run_multi_capability(capsys):
    code, out = _run_cli(
        capsys,
        "review this PR and run a security scan",
        "--repo",
        str(FIXTURE_REPO),
        "--dry-run",
    )
    assert code == 0
    assert out["dry_run"] is True
    assert out["orchestration"] == "multi-agent"
    assert set(out["capabilities"]) >= {"pr_review", "security_review"}


def test_cli_run_reads_diff_file(tmp_path, capsys):
    diff_path = tmp_path / "pr.diff"
    diff_path.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")
    code, out = _run_cli(
        capsys,
        "review this pull request",
        "--repo",
        str(FIXTURE_REPO),
        "--diff",
        str(diff_path),
    )
    assert code == 0
    assert "pr_review" in out["capabilities"]
    assert out["result"] is not None


def test_cli_run_replay_requires_replay_dir(capsys):
    # Error path: no JSON on stdout, non-zero exit, message on stderr.
    code = cli_main(["run", "fix bug: login button fails", "--mode", "replay"])
    captured = capsys.readouterr()
    assert code != 0
    assert "replay" in captured.err.lower()


def test_mcp_run_task_fake():
    pytest.importorskip("mcp")
    from upgradelens.mcp import server as mcp_server

    out = mcp_server.run_task("fix bug: login button fails")
    assert out["capabilities"] == ["issue_repair"]
    assert out["error"] is None
    assert out["result"] is not None


def test_mcp_run_task_dry_run_unknown():
    pytest.importorskip("mcp")
    from upgradelens.mcp import server as mcp_server

    out = mcp_server.run_task("hello there, nice weather today", dry_run=True)
    assert out["dry_run"] is True
    assert out["capabilities"] == []
    assert out["degradations"] == ["no-capability-matched"]
