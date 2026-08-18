"""Offline tests for the M4 GitHub PR bot.

Covers the offline-safe pieces: ``build_pr_report`` (fake capability run) and
``pr_diff`` (with a stubbed HTTP layer). The live path that actually posts a
comment to GitHub is intentionally NOT exercised here -- it is deferred to live
verification.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from upgradelens.bots.github_pr_bot import build_pr_report
from upgradelens.tools.github import pr_diff

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = ROOT / "tests/fixtures/eval/pydantic_field_validator/repo"


def test_build_pr_report_fake():
    report = build_pr_report(str(FIXTURE_REPO), "", mode="fake")
    assert "UpgradeLens PR 审查报告" in report
    assert "### PR 审查" in report
    assert "### 安全审查" in report


def test_pr_diff_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    captured: dict[str, object] = {}

    def _fake_urlopen(request: urllib.request.Request, timeout: float = 30) -> _FakeResp:
        captured["accept"] = request.headers.get("Accept", "")
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        return _FakeResp(b"diff --git a/main.py b/main.py\n")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    out = pr_diff("octo/example", 42, token="secret-token")
    assert out == "diff --git a/main.py b/main.py\n"
    assert "application/vnd.github.v3.diff" in str(captured["accept"])
    assert "repos/octo/example/pulls/42" in str(captured["url"])
    assert captured["auth"] == "Bearer secret-token"
