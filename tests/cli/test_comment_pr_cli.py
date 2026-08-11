"""CLI tests for the ``comment-pr`` subcommand (dry-run, fully offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.cli import EXIT_OK, main

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pydantic_usage"


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_comment_pr_dry_run_renders_report(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(
        capsys,
        "comment-pr",
        "--repo",
        str(FIXTURE),
        "--dependency",
        "pydantic",
        "--mode",
        "fake",
        "--slug",
        "owner/repo",
        "--pr",
        "1",
        "--dry-run",
    )
    assert code == EXIT_OK, err
    assert out.startswith("# 升级影响报告 — pydantic")
    assert "## 结论" in out
    assert "dry-run" in err


def test_comment_pr_rejects_missing_required_args() -> None:
    # argparse exits with code 2 when required --slug/--pr are absent.
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "comment-pr",
                "--repo",
                str(FIXTURE),
                "--dependency",
                "pydantic",
                "--mode",
                "fake",
                "--dry-run",
            ]
        )
    assert exc.value.code == 2
