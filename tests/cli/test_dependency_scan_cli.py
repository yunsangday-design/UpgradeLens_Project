"""CLI tests (plan sections 8.10 and 11.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upgradelens.cli import EXIT_INVALID_REQUEST, EXIT_OK, EXIT_USAGE, main

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object], str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_scan_matches_fixture_contract(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = FIXTURES_ROOT / "pydantic_validator"
    expected = json.loads((fixture / "expected_dependency_scan.json").read_text(encoding="utf-8"))

    code, payload, err = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(fixture / "repo"),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
    )

    assert code == EXIT_OK
    assert payload == expected
    assert err == ""


def test_scan_with_explicit_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = FIXTURES_ROOT / "pydantic_serialization"
    code, payload, _ = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(fixture / "repo"),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
        "--manifest",
        "requirements.txt",
    )

    assert code == EXIT_OK
    assert payload["status"] == "ambiguous"
    assert len(payload["declarations"]) == 1  # type: ignore[arg-type]


def test_not_found_still_exits_zero(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    code, payload, _ = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(tmp_path),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
    )
    assert code == EXIT_OK
    assert payload["status"] == "not_found"


def test_missing_repository_is_invalid_request(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "nope"
    code, payload, err = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(missing),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
    )

    assert code == EXIT_INVALID_REQUEST
    assert payload["status"] == "invalid"
    assert payload["errors"][0]["code"] == "invalid_request"  # type: ignore[index]
    assert "invalid request" in err


def test_invalid_target_version_is_invalid_request(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, payload, _ = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(tmp_path),
        "--dependency",
        "pydantic",
        "--target-version",
        "not-a-version",
    )
    assert code == EXIT_INVALID_REQUEST
    assert payload["status"] == "invalid"


def test_invalid_output_never_leaks_absolute_paths(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "definitely-missing"
    _, payload, _ = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(missing),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
    )
    serialized = json.dumps(payload)
    assert str(missing) not in serialized
    assert str(tmp_path) not in serialized


def test_scan_output_never_leaks_absolute_paths(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = FIXTURES_ROOT / "pydantic_serialization"
    _, payload, _ = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(fixture / "repo"),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
    )
    serialized = json.dumps(payload)
    assert str(fixture) not in serialized
    for declaration in payload["declarations"]:  # type: ignore[attr-defined]
        assert "\\" not in declaration["path"]
        assert not declaration["path"].startswith("/")


def test_utf8_repository_path_is_supported(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repo = tmp_path / "我的 项目"
    repo.mkdir()
    (repo / "requirements.txt").write_text("# 中文注释\npydantic==1.10.13\n", encoding="utf-8")

    code, payload, _ = _run(
        capsys,
        "scan-dependency",
        "--repo",
        str(repo),
        "--dependency",
        "pydantic",
        "--target-version",
        "2.0.0",
    )

    assert code == EXIT_OK
    assert payload["status"] == "resolved"
    assert payload["declarations"][0]["path"] == "requirements.txt"  # type: ignore[index]


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("scan-dependency",),
        ("scan-dependency", "--repo", "."),
        ("unknown-command",),
    ],
)
def test_usage_errors_exit_two(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == EXIT_USAGE


def test_output_is_pretty_printed_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")
    main(
        (
            "scan-dependency",
            "--repo",
            str(tmp_path),
            "--dependency",
            "pydantic",
            "--target-version",
            "2.0.0",
        )
    )
    out = capsys.readouterr().out
    assert out.startswith("{\n")
    assert out.endswith("}\n")
    assert '\n  "schema_version": "1.0",' in out


def test_scan_code_reports_usages(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = FIXTURES_ROOT / "pydantic_usage"
    code, payload, err = _run(
        capsys,
        "scan-code",
        "--repo",
        str(fixture / "repo"),
        "--dependency",
        "pydantic",
    )

    assert code == EXIT_OK
    assert payload["dependency_name"] == "pydantic"
    assert payload["schema_version"] == "1.0"
    assert payload["summary"]["usage_count"] > 0
    assert any(u["kind"] == "class_base" for u in payload["usages"])
    assert any(u["kind"] == "decorator" for u in payload["usages"])
    assert any(u["is_test_code"] for u in payload["usages"])
    assert err == ""


def test_scan_code_output_never_leaks_absolute_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = FIXTURES_ROOT / "pydantic_usage"
    _, payload, _ = _run(
        capsys,
        "scan-code",
        "--repo",
        str(fixture / "repo"),
        "--dependency",
        "pydantic",
    )
    serialized = json.dumps(payload)
    assert str(fixture) not in serialized
    for usage in payload["usages"]:
        assert not usage["path"].startswith("/")
        assert "\\" not in usage["path"]
