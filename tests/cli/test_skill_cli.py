"""CLI tests for the stage 3 skill subcommands (plan sections 8.10, 3)."""

from __future__ import annotations

import json

from upgradelens.cli import EXIT_INVALID_REQUEST, EXIT_OK, main


def _run(capsys, *argv: str):
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_list_skills_reports_builtin_packs(capsys) -> None:
    code, payload, err = _run(capsys, "list-skills")
    assert code == EXIT_OK
    ids = {s["skill_id"] for s in payload["skills"]}
    assert "pydantic_v1_to_v2" in ids
    assert "generic_python_dependency" in ids
    assert payload["generic_skill_id"] == "generic_python_dependency"
    # LS-4: the command still runs, but clearly warns it is deprecated.
    assert "DEPRECATED" in err
    assert "legacy compatibility" in err


def test_resolve_skill_warns_deprecated(capsys, monkeypatch) -> None:
    monkeypatch.delenv("UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION", raising=False)
    code, payload, err = _run(
        capsys, "resolve-skill", "--dependency", "pydantic", "--target-version", "2.0.0"
    )
    assert code == EXIT_OK
    assert payload["skill_id"] == "pydantic_v1_to_v2"
    assert "DEPRECATED" in err


def test_list_skills_accepts_custom_base_dir(capsys, tmp_path) -> None:
    skill_dir = tmp_path / "packs" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "skill_id: demo\npackage_names:\n  - demo\ntarget_version_spec: '>=1,<2'\n",
        encoding="utf-8",
    )
    code, payload, _ = _run(capsys, "list-skills", "--base-dir", str(tmp_path / "packs"))
    assert code == EXIT_OK
    assert {s["skill_id"] for s in payload["skills"]} == {"demo"}


def test_resolve_pydantic_selects_dedicated(capsys, monkeypatch) -> None:
    # pin the legacy-selection switch OFF: this documents default behaviour
    # (LS-1 acceptance runs the suite with the switch set, where dedicated
    # packs are skipped and selection falls back to generic).
    monkeypatch.delenv("UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION", raising=False)
    code, payload, _ = _run(
        capsys, "resolve-skill", "--dependency", "pydantic", "--target-version", "2.0.0"
    )
    assert code == EXIT_OK
    assert payload["skill_id"] == "pydantic_v1_to_v2"
    assert payload["is_generic"] is False
    assert payload["matched_by"] == "version_range"
    assert payload["skill_version"] == "1.0.0"


def test_resolve_unknown_falls_back_to_generic(capsys) -> None:
    code, payload, _ = _run(
        capsys, "resolve-skill", "--dependency", "requests", "--target-version", "2.31.0"
    )
    assert code == EXIT_OK
    assert payload["is_generic"] is True
    assert payload["matched_by"] == "generic_fallback"
    assert payload["capability_note"]


def test_resolve_invalid_version_is_invalid_request(capsys) -> None:
    code, payload, _ = _run(
        capsys, "resolve-skill", "--dependency", "pydantic", "--target-version", "not-a-version"
    )
    assert code == EXIT_INVALID_REQUEST
    assert payload["status"] == "invalid"
