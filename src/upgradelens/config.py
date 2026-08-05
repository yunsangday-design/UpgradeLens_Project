"""Minimal configuration for UpgradeLens stage 0.

Stage 0 only needs cross-platform-friendly settings. Secrets such as LLM API
keys are declared but never required at this stage; callers that need a secret
must request it through :meth:`Settings.require_secret`, which raises a clear
error without echoing the sensitive value into logs or traces.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment and an optional ``.env``.

    Only non-sensitive defaults are declared here. Stage 0 must run fully
    offline without any secret present.
    """

    model_config = SettingsConfigDict(
        env_prefix="UPGRADELENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=False,
    )

    app_name: str = "upgradelens"
    log_level: str = "INFO"
    # Optional secret placeholder. Not required until the LLM stage.
    api_key: SecretStr | None = Field(default=None)

    # Stage 5 model gateway configuration (OpenAI-compatible, e.g. 阿里云百炼).
    model_mode: str = "fake"  # fake | replay | live
    model_base_url: str = ""  # OpenAI-compatible base url
    model_name: str = "qwen-plus"
    model_max_total_tokens: int = 20000
    # Secret API key used only in live mode. Not required for fake/replay.
    model_api_key: SecretStr | None = Field(default=None)

    def require_secret(self, name: str = "api_key") -> SecretStr:
        """Return a secret or raise a clear, non-leaking error.

        The error intentionally avoids echoing the secret value or the raw
        environment content.
        """
        value = getattr(self, name, None)
        if not isinstance(value, SecretStr) or not value.get_secret_value():
            raise ValueError(
                f"Missing required secret '{name}'. "
                "Set it via environment (UPGRADELENS_<NAME>) or .env; "
                "it is not needed for stage 0-1."
            )
        return value
