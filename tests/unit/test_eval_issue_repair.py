"""Offline tests for the B2 issue-repair gold set and its CLI command."""

from __future__ import annotations

import json

from upgradelens.cli import main as cli_main
from upgradelens.eval.issue_repair_eval import run_issue_repair_eval


def test_b2_gold_set_full_metrics():
    report = run_issue_repair_eval()
    # 11 locatable cases must all hit file:symbol via the deterministic scan.
    assert report.locatable_cases == 11
    assert report.root_cause_hits == 11
    assert report.root_cause_hit_rate == 1.0
    # The information-less report must NOT fabricate a root cause.
    assert report.clarification_cases == 1
    assert report.clarification_correct == 1
    # The fake pipeline (deterministic scan + canned model step) stays green.
    assert report.pipeline_ok == report.total_cases == 12
    # The bundled repro tests must be red against the broken fixture repo.
    assert report.repro_fails_before_fix is True


def test_b2_scoreboard_lists_every_case():
    md = run_issue_repair_eval(run_repro=False).scoreboard_md
    assert "Issue-Repair Gold-Set Evaluation (B2)" in md
    for name in (
        "login-none-password",
        "cart-total-empty-cart",
        "get-config-missing-key",
        "vague-report-no-clue",
    ):
        assert name in md


def test_b2_cli_gate_and_outputs(tmp_path, capsys):
    json_path = tmp_path / "b2.json"
    md_path = tmp_path / "b2.md"
    code = cli_main(
        [
            "eval-issue-repair",
            "--fail-under",
            "1.0",
            "--json",
            str(json_path),
            "--md",
            str(md_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Root-cause hit rate" in out
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["root_cause_hit_rate"] == 1.0
    assert payload["repro_fails_before_fix"] is True
    assert len(payload["cases"]) == 12
    assert "Issue-Repair Gold-Set" in md_path.read_text(encoding="utf-8")
