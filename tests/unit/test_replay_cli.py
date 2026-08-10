"""End-to-end tests for the LLM replay closed loop (CLI wiring).

These exercise the real command dispatch so the ``--replay-dir`` plumbing and
the ``seed-replay`` command are covered, not just the gateway unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from upgradelens.cli import EXIT_INVALID_REQUEST, EXIT_OK, main

TESTS = Path(__file__).resolve().parents[1]
FIXTURE = TESTS / "fixtures" / "pydantic_usage"


def test_seed_replay_writes_node_responses(tmp_path: Path) -> None:
    replay_dir = tmp_path / "replay"
    rc = main(
        [
            "seed-replay",
            "--repo",
            str(FIXTURE),
            "--dependency",
            "pydantic",
            "--out",
            str(replay_dir),
        ]
    )
    assert rc == EXIT_OK
    # The impact analyzer node is the one the assess pipeline replays.
    assert (replay_dir / "impact_analyzer.json").exists()


def test_replay_closed_loop_produces_verified_report(
    tmp_path: Path, capsys: object
) -> None:
    replay_dir = tmp_path / "replay"
    rc = main(
        [
            "seed-replay",
            "--repo",
            str(FIXTURE),
            "--dependency",
            "pydantic",
            "--out",
            str(replay_dir),
        ]
    )
    assert rc == EXIT_OK
    capsys.readouterr()  # clear seed output

    rc2 = main(
        [
            "assess",
            "--repo",
            str(FIXTURE),
            "--dependency",
            "pydantic",
            "--mode",
            "replay",
            "--replay-dir",
            str(replay_dir),
            "--format",
            "json",
        ]
    )
    assert rc2 == EXIT_OK
    out = capsys.readouterr().out
    report = json.loads(out)

    # The recorded (canned, evidence-anchored) model response is replayed
    # faithfully: the risk ids the verifier sees must equal what was recorded.
    # (The verifier re-checks citations against the live bundle, so a risk that
    # cited the demo's synthetic doc chunk is downgraded to PARTIAL rather than
    # VERIFIED -- that is the anti-hallucination gate working as designed.)
    recorded = json.loads((replay_dir / "impact_analyzer.json").read_text())
    recorded_ids = {r["risk_id"] for r in recorded["output"]["risks"]}
    assert recorded_ids
    replayed_ids = {
        r["risk_id"] for r in report["verified_risks"] + report["degraded_risks"]
    }
    assert replayed_ids == recorded_ids


def test_replay_mode_requires_replay_dir() -> None:
    rc = main(
        [
            "assess",
            "--repo",
            str(FIXTURE),
            "--dependency",
            "pydantic",
            "--mode",
            "replay",
        ]
    )
    assert rc == EXIT_INVALID_REQUEST
