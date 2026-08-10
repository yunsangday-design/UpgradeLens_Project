"""Offline tests for the stage 6.1 CI gate.

The gate only blocks on *verified* high/critical risk. Degraded or unverified
findings must never block an upgrade.
"""

from __future__ import annotations

import json

from upgradelens.cli import EXIT_GATE_BLOCKED, EXIT_INVALID_REQUEST, EXIT_OK, main
from upgradelens.gate import (
    DEFAULT_BLOCKING_SEVERITIES,
    GateResult,
    gate_report,
    is_blocking_severity,
)
from upgradelens.verify.models import EvidenceStatus, VerifiedReport, VerifiedRisk


def _risk(risk_id: str, *, verified: bool, severity: str) -> VerifiedRisk:
    return VerifiedRisk(
        risk_id=risk_id,
        title=f"risk {risk_id}",
        status=EvidenceStatus.VERIFIED if verified else EvidenceStatus.PARTIALLY_VERIFIED,
        severity=severity,
        model_severity=severity,
    )


def _report(*risks: VerifiedRisk) -> VerifiedReport:
    return VerifiedReport(verified_risks=list(risks))


def test_blocks_on_verified_high():
    report = _report(_risk("r1", verified=True, severity="high"))
    result = gate_report(report)
    assert isinstance(result, GateResult)
    assert result.block is True
    assert result.verified_blocking == 1
    assert result.reasons[0].risk_id == "r1"


def test_does_not_block_on_unverified_high():
    report = _report(_risk("r1", verified=False, severity="high"))
    result = gate_report(report)
    assert result.block is False
    assert result.verified_blocking == 0


def test_does_not_block_on_verified_medium_by_default():
    report = _report(_risk("r1", verified=True, severity="medium"))
    result = gate_report(report)
    assert result.block is False


def test_custom_block_on_medium():
    report = _report(_risk("r1", verified=True, severity="medium"))
    result = gate_report(report, block_on={"medium"})
    assert result.block is True


def test_empty_report_is_ok():
    result = gate_report(_report())
    assert result.block is False
    assert result.summary.startswith("GATE OK")


def test_only_verified_counted_in_summary():
    report = _report(
        _risk("v1", verified=True, severity="critical"),
        _risk("p1", verified=False, severity="critical"),
        _risk("m1", verified=True, severity="low"),
    )
    result = gate_report(report)
    assert result.block is True
    assert result.checked == 3
    assert result.verified_blocking == 1


def test_is_blocking_severity_normalizes():
    assert is_blocking_severity("HIGH", block_on=DEFAULT_BLOCKING_SEVERITIES)
    assert is_blocking_severity("critical", block_on=DEFAULT_BLOCKING_SEVERITIES)
    assert not is_blocking_severity("low", block_on=DEFAULT_BLOCKING_SEVERITIES)
    assert is_blocking_severity("8", block_on={"8"})


def _write_report(path, report: VerifiedReport) -> None:
    path.write_text(report.model_dump_json(), encoding="utf-8")


def test_cli_gate_blocks(tmp_path, capsys):
    report = _report(_risk("r1", verified=True, severity="high"))
    path = tmp_path / "report.json"
    _write_report(path, report)
    rc = main(["gate", "--report", str(path)])
    assert rc == EXIT_GATE_BLOCKED
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_gate_ok(tmp_path, capsys):
    report = _report(_risk("r1", verified=True, severity="low"))
    path = tmp_path / "report.json"
    _write_report(path, report)
    rc = main(["gate", "--report", str(path)])
    assert rc == EXIT_OK


def test_cli_gate_accepts_raw_assess_artifact(tmp_path):
    # `assess --raw` emits an AssessmentResult with a nested "verified" key.
    report = _report(_risk("r1", verified=True, severity="critical"))
    artifact = {"verified": json.loads(report.model_dump_json())}
    path = tmp_path / "run.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    rc = main(["gate", "--report", str(path), "--format", "json"])
    assert rc == EXIT_GATE_BLOCKED


def test_cli_gate_missing_report(tmp_path):
    rc = main(["gate", "--report", str(tmp_path / "nope.json")])
    assert rc == EXIT_INVALID_REQUEST
