"""Tests for schema bootstrap (Alembic migration) and code-evidence persistence."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from upgradelens.analyzers import scan_code_evidence
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.db.repository import code_usage_count, persist_code_report

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_upgrade_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")

    eng = engine_for(db)
    with eng.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert {
        "doc_sources",
        "doc_chunks",
        "retrieval_runs",
        "code_evidence",
        "doc_chunks_fts",
    }.issubset(tables)


def test_persist_and_count_code_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "models.py").write_text(
        "import pydantic\n\n\nclass M(pydantic.BaseModel):\n    x: int\n",
        encoding="utf-8",
    )
    report = scan_code_evidence(repo, "pydantic")
    assert report.usages

    eng = engine_for(tmp_path / "upgradelens.db")
    init_db(eng)
    session = session_for(eng)()
    try:
        assert persist_code_report(session, report) == len(report.usages)
        assert code_usage_count(session, "pydantic") == len(report.usages)
        assert code_usage_count(session, "nonexistent") == 0
    finally:
        session.close()
