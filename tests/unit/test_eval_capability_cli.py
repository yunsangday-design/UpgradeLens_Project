"""Offline tests for the A5 ``eval-capability`` CLI command."""

from __future__ import annotations

import json

from upgradelens.cli import main as cli_main


def test_eval_capability_all(capsys):
    code = cli_main(["eval-capability"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Capability Gold-Set Evaluation" in out
    # Four non-upgrade capabilities each contribute rows to the scoreboard.
    for kind in ("pr_review", "security_review", "issue_repair", "breaking_change"):
        assert kind in out


def test_eval_capability_kind_filter_and_gate(capsys):
    code = cli_main(["eval-capability", "--kind", "pr_review", "--fail-under", "1.0"])
    assert code == 0
    out = capsys.readouterr().out
    assert "pr_review" in out
    assert "security_review" not in out.split("| Capability |")[-1]


def test_eval_capability_writes_json(tmp_path, capsys):
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "scoreboard.md"
    code = cli_main(
        ["eval-capability", "--json", str(json_path), "--md", str(md_path)]
    )
    assert code == 0
    capsys.readouterr()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["total_cases"] >= 10
    assert payload["overall_pass_rate"] == 1.0
    assert payload["hallucination_free_rate"] == 1.0
    assert len(payload["cases"]) == payload["total_cases"]
    assert "Capability Gold-Set Evaluation" in md_path.read_text(encoding="utf-8")
