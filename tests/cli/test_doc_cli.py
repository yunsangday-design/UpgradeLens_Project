"""CLI tests for the stage 4 documentation subcommands."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from upgradelens.cli import EXIT_OK, main
from upgradelens.db import models


def _run(capsys, *argv: str):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ingest_and_retrieve_docs(capsys, tmp_path: Path) -> None:
    db = tmp_path / "upgradelens.db"
    code, out, err = _run(capsys, "ingest-docs", "--db", str(db), "--skill", "pydantic_v1_to_v2")
    assert code == EXIT_OK, err
    assert "pydantic_migration_guide" in out

    code, out, err = _run(
        capsys,
        "retrieve-docs",
        "--db",
        str(db),
        "--source",
        "pydantic_migration_guide",
        "--query",
        "validator",
    )
    assert code == EXIT_OK, err
    assert "@validator" in out
    assert "https://docs.pydantic.dev/latest/migration/" in out


def test_scan_code_persists_to_db(capsys, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "models.py").write_text(
        "import pydantic\n\n\nclass M(pydantic.BaseModel):\n    x: int\n",
        encoding="utf-8",
    )
    db = tmp_path / "upgradelens.db"
    code, out, err = _run(
        capsys, "scan-code", "--repo", str(repo), "--dependency", "pydantic", "--db", str(db)
    )
    assert code == EXIT_OK, err
    assert "pydantic" in out

    from upgradelens.db.database import engine_for, session_for

    eng = engine_for(db)
    session = session_for(eng)()
    try:
        count = session.execute(select(func.count(models.CodeEvidenceRow.id))).scalar_one()
        assert count >= 1
    finally:
        session.close()
