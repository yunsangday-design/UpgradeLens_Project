from __future__ import annotations

import pytest

from upgradelens.config import Settings


def test_default_settings_load_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.environ", {})
    settings = Settings()
    assert settings.app_name == "upgradelens"
    assert settings.api_key is None


def test_env_prefix_overrides_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPGRADELENS_LOG_LEVEL", "DEBUG")
    settings = Settings()
    assert settings.log_level == "DEBUG"


def test_require_secret_raises_clear_error_without_leak() -> None:
    settings = Settings()
    with pytest.raises(ValueError) as exc:
        settings.require_secret("api_key")
    message = str(exc.value)
    assert "api_key" in message
    assert "not needed for stage 0-1" in message


def test_require_secret_returns_secret_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPGRADELENS_API_KEY", "top-secret-value")
    settings = Settings()
    secret = settings.require_secret("api_key")
    assert secret.get_secret_value() == "top-secret-value"
