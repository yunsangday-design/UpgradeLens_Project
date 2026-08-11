"""CLI tests for the `assess` subcommand (fake mode, fully offline).

Stage 5 produced a raw model report; stage 6 wraps it in a verified report, so
the default output shape is `verified-report/1`. `--raw` still exposes the
unverified document for debugging.
"""

from __future__ import annotations

import json
from pathlib import Path

from upgradelens.cli import EXIT_OK, main

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pydantic_usage"


def _run(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_fixture_repo_exists() -> None:
    """Guards the path arithmetic above; a wrong root silently scans nothing."""
    assert FIXTURE.is_dir()
    assert (FIXTURE / "repo").is_dir()


def test_assess_fake_without_db(capsys) -> None:
    code, out, err = _run(
        capsys,
        "assess",
        "--repo",
        str(FIXTURE),
        "--dependency",
        "pydantic",
        "--mode",
        "fake",
    )
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["schema_version"] == "verified-report/1"
    assert payload["target_dependency"] == "pydantic"
    # Without --db there is no documentation evidence, so the run is degraded
    # and nothing may be promoted to "verified".
    assert payload["partial"] is True
    assert payload["verified_risks"] == []


def test_assess_raw_emits_unverified_report(capsys) -> None:
    code, out, err = _run(
        capsys,
        "assess",
        "--repo",
        str(FIXTURE),
        "--dependency",
        "pydantic",
        "--mode",
        "fake",
        "--raw",
    )
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert "risks" in payload
    assert "static" in payload


def test_assess_fake_with_ingested_docs(capsys, tmp_path: Path) -> None:
    db = tmp_path / "upgradelens.db"
    code, out, err = _run(capsys, "ingest-docs", "--db", str(db), "--skill", "pydantic_v1_to_v2")
    assert code == EXIT_OK, err

    code, out, err = _run(
        capsys,
        "assess",
        "--repo",
        str(FIXTURE),
        "--dependency",
        "pydantic",
        "--mode",
        "fake",
        "--db",
        str(db),
    )
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["schema_version"] == "verified-report/1"
    assert payload["conclusion"] in {"impacted", "no_impact", "evidence_insufficient"}
    assert "evidence_summary" in payload
    # Every cited evidence id must exist in the bundle.
    for risk in payload["verified_risks"] + payload["degraded_risks"]:
        assert risk["unknown_evidence_ids"] == []


def test_assess_markdown_format(capsys, tmp_path: Path) -> None:
    db = tmp_path / "upgradelens.db"
    code, _, err = _run(capsys, "ingest-docs", "--db", str(db), "--skill", "pydantic_v1_to_v2")
    assert code == EXIT_OK, err

    code, out, err = _run(
        capsys,
        "assess",
        "--repo",
        str(FIXTURE),
        "--dependency",
        "pydantic",
        "--mode",
        "fake",
        "--db",
        str(db),
        "--format",
        "md",
    )
    assert code == EXIT_OK, err
    assert out.startswith("# 升级影响报告 — pydantic")
    assert "## 结论" in out
    assert "## 已验证风险" in out
    assert "## 推荐测试" in out
