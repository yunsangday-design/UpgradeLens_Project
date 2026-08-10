"""Verify that the model gateway's *live* (real LLM) configuration is wired.

The LLM integration itself landed in Stage 5 (model gateway). These tests only
prove that the configuration layer exposes the right knobs so a real
OpenAI-compatible endpoint (e.g. 阿里云百炼) can be reached by environment
configuration alone -- no secret is needed to assert the wiring.
"""

from pathlib import Path

import pytest

from upgradelens.config import Settings


def test_settings_reads_live_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPGRADELENS_MODEL_MODE", "live")
    monkeypatch.setenv(
        "UPGRADELENS_MODEL_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("UPGRADELENS_MODEL_NAME", "qwen-flash")
    monkeypatch.setenv("UPGRADELENS_MODEL_API_KEY", "sk-test-not-real")

    s = Settings()
    assert s.model_mode == "live"
    assert s.model_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s.model_name == "qwen-flash"
    assert s.model_api_key is not None
    assert s.model_api_key.get_secret_value() == "sk-test-not-real"


def test_settings_default_is_fake_and_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate from a developer .env that may set UPGRADELENS_MODEL_MODE=live.
    monkeypatch.chdir(tmp_path)
    # Ensure no stray live-mode env leaks into the offline default.
    for name in (
        "UPGRADELENS_MODEL_MODE",
        "UPGRADELENS_MODEL_BASE_URL",
        "UPGRADELENS_MODEL_NAME",
        "UPGRADELENS_MODEL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    s = Settings()
    assert s.model_mode == "fake"
    assert s.model_api_key is None
