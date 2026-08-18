"""CLI/MCP tests for the LS-4 / SK-1 post-skill surface commands.

The legacy ``list-skills`` / ``resolve-skill`` commands remain as compatibility
shims (LS-4) but the supported surface is now:

* ``list-agent-skills`` / ``resolve-agent-skill`` (SK-1)
* ``list-corpus-sources`` (LS-2)
* ``resolve-capability`` (B5; LS-3 migrated)
"""

from __future__ import annotations

import json

from upgradelens.cli import EXIT_OK, main


def _run(capsys, *argv: str):
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_list_agent_skills_returns_all_four_builtin(capsys):
    code, payload, _ = _run(capsys, "list-agent-skills")
    assert code == EXIT_OK
    ids = {s["skill_id"] for s in payload["agent_skills"]}
    assert ids == {
        "evidence-grounded-review",
        "safe-dependency-migration",
        "systematic-issue-diagnosis",
        "verification-before-completion",
    }
    # Every skill declares which capability kinds it applies to.
    for s in payload["agent_skills"]:
        assert s["applies_to"], s
        assert s["version"], s
        assert s["language"], s


def test_resolve_agent_skill_routing_contract(capsys):
    code, payload, _ = _run(capsys, "resolve-agent-skill", "--kind", "dependency_upgrade")
    assert code == EXIT_OK
    assert payload["skill_id"] == "safe-dependency-migration"
    assert payload["matched_by"] == "routing_contract"
    assert "Steps:" not in payload  # only structured fields, not the L1 block
    assert payload["steps"]


def test_resolve_agent_skill_negative_kind_returns_none(capsys):
    code, payload, _ = _run(capsys, "resolve-agent-skill", "--kind", "chat_summary")
    assert code == EXIT_OK
    assert payload["skill_id"] is None
    assert payload["matched_by"] == "none"


def test_resolve_agent_skill_locale_zh_prefers_cn_variant(capsys):
    code, payload, _ = _run(
        capsys, "resolve-agent-skill", "--kind", "pr_review", "--locale", "zh-CN"
    )
    assert code == EXIT_OK
    # cn variant exists; resolver returns it
    assert payload["skill_id"] == "evidence-grounded-review"
    # metadata reports the language variant that was loaded
    assert payload["language"] in ("en", "cn")


def test_list_corpus_sources_returns_builtin_packages(capsys):
    code, payload, _ = _run(capsys, "list-corpus-sources")
    assert code == EXIT_OK
    pkgs = {s["package_name"] for s in payload["sources"]}
    # the shared corpus must contain at least the two migrated packages
    assert "pydantic" in pkgs
    for s in payload["sources"]:
        assert s["id"], s
        assert s["trust_level"], s


def test_resolve_capability_uses_migrated_packs(capsys, monkeypatch):
    # The new path resolves from capabilities/transformations/*.yaml; the
    # legacy Skill path is no longer consulted.
    monkeypatch.delenv("UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION", raising=False)
    for dep, expected in [("pydantic", "pydantic_v1_to_v2"), ("sqlalchemy", "sqlalchemy_v1_to_v2")]:
        code, payload, _ = _run(
            capsys, "resolve-capability", "--dependency", dep, "--target-version", "2.0"
        )
        assert code == EXIT_OK
        assert payload["capability_id"] == expected
        assert payload["allow_patch_draft"] is True
        assert payload["patch_rules"], payload


def test_resolve_capability_unknown_dependency_returns_null(capsys):
    code, payload, _ = _run(
        capsys, "resolve-capability", "--dependency", "no_such_pkg", "--target-version", "2.0"
    )
    assert code == EXIT_OK
    assert payload["capability_id"] is None
