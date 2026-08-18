"""Offline tests for the UpgradeLens MCP server tools.

These exercise the same code paths as the CLI subcommands but through the
``upgradelens.mcp.server`` tool functions, so they double as a contract check
that the MCP surface matches the CLI JSON output. No network or API key is
required: ``assess`` runs in ``fake`` mode and ``run_eval`` is fully offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from upgradelens.mcp import server as mcp_server  # noqa: E402


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "model.py").write_text(
        "import pydantic\n\nclass User(pydantic.BaseModel):\n    name: str\n    age: int = 0\n",
        encoding="utf-8",
    )
    return repo


def _assert_jsonable(value: object) -> None:
    assert isinstance(value, dict)
    json.dumps(value, ensure_ascii=False)


def test_list_skills():
    catalog = mcp_server.list_skills()
    _assert_jsonable(catalog)
    assert "skills" in catalog
    # LS-4: legacy skill tools keep working but clearly flag their deprecation.
    assert "DEPRECATED" in catalog["deprecation"]


def test_resolve_skill():
    selection = mcp_server.resolve_skill("pydantic", "2.0")
    _assert_jsonable(selection)
    assert "DEPRECATED" in selection["deprecation"]


def test_scan_code(tmp_path):
    repo = _make_repo(tmp_path)
    report = mcp_server.scan_code(str(repo), "pydantic")
    _assert_jsonable(report)
    assert report["dependency_name"] == "pydantic"


def test_assess_fake_offline(tmp_path):
    repo = _make_repo(tmp_path)
    result = mcp_server.assess(repo=str(repo), dependency="pydantic", mode="fake")
    _assert_jsonable(result)
    assert "verified_risks" in result


def test_ingest_and_retrieve_docs(tmp_path):
    db = tmp_path / "docs.db"
    ingested = mcp_server.ingest_docs(str(db), skill="pydantic_v1_to_v2")
    _assert_jsonable(ingested)
    assert ingested["skill_id"] == "pydantic_v1_to_v2"
    assert ingested["ingested"]

    run = mcp_server.retrieve_docs(str(db), source="pydantic_v1_to_v2", query="validator")
    _assert_jsonable(run)


def test_run_eval_offline():
    result = mcp_server.run_eval()
    _assert_jsonable(result)
    assert "baselines" in result
    assert "cases" in result


def test_list_agent_skills():
    catalog = mcp_server.list_agent_skills()
    _assert_jsonable(catalog)
    ids = {s["skill_id"] for s in catalog["agent_skills"]}
    assert ids == {
        "evidence-grounded-review",
        "safe-dependency-migration",
        "systematic-issue-diagnosis",
        "verification-before-completion",
    }


def test_resolve_agent_skill():
    for kind, expected in [
        ("dependency_upgrade", "safe-dependency-migration"),
        ("pr_review", "evidence-grounded-review"),
        ("chat_summary", None),
    ]:
        result = mcp_server.resolve_agent_skill(kind)
        _assert_jsonable(result)
        assert result["skill_id"] == expected
        assert result["matched_by"] in ("routing_contract", "fallback", "none")


def test_resolve_agent_skill_locale_zh():
    result = mcp_server.resolve_agent_skill("pr_review", locale="zh-CN")
    _assert_jsonable(result)
    assert result["skill_id"] == "evidence-grounded-review"


def test_list_corpus_sources():
    sources = mcp_server.list_corpus_sources()
    _assert_jsonable(sources)
    pkgs = {s["package_name"] for s in sources["sources"]}
    assert "pydantic" in pkgs


def test_resolve_capability_uses_migrated_pack():
    """LS-3: resolve_capability now resolves from the YAML pack, not the Skill."""
    for dep, expected in [
        ("pydantic", "pydantic_v1_to_v2"),
        ("sqlalchemy", "sqlalchemy_v1_to_v2"),
        ("no_such_pkg", None),
    ]:
        result = mcp_server.resolve_capability(dep, "2.0")
        _assert_jsonable(result)
        assert result["capability_id"] == expected
