"""CLI test for the stage 5 `assess` subcommand (fake mode, offline)."""

from __future__ import annotations

from pathlib import Path

from upgradelens.cli import EXIT_OK, main

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "pydantic_usage"


def _run(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


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
    assert '"risks"' in out
    assert '"static"' in out
    assert '"target_dependency"' in out


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
    assert '"risks"' in out
    assert '"evidence_summary"' in out
