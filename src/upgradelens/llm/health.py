"""Connectivity self-check for the model gateway.

The assessment graph deliberately degrades to a static report when the model is
unreachable, which is safe but silent: a misconfigured key looks exactly like a
repo with no model-worthy findings. This module provides the smallest possible
structured call so operators can answer one question directly -- *is the
configured endpoint reachable and able to return structured output?* -- and it
is what ``upgradelens llm-check`` runs.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from upgradelens.llm.gateway import (
    BudgetExceededError,
    CompletionTransport,
    ModelConfig,
    ModelGateway,
    ModelMode,
    ModelUnavailableError,
    StructuredOutputError,
)

__all__ = ["ModelHealth", "ProbeAnswer", "check_model"]

#: Deliberately tiny: the probe should cost a few tokens, not a real analysis.
PROBE_PROMPT = (
    "You are a connectivity probe for the UpgradeLens agent. "
    "Return ok=true and set message to the single word: pong."
)

_NOTES = {
    ModelMode.FAKE: (
        "mode=fake: no API call was made, the response is canned. "
        "Use --mode live to probe the real endpoint."
    ),
    ModelMode.REPLAY: (
        "mode=replay: the response was read from disk, no API call was made."
    ),
    ModelMode.LIVE: (
        "mode=live: the configured endpoint is used directly, "
        "no canned or replayed data is involved."
    ),
}


class ProbeAnswer(BaseModel):
    """Minimal schema the endpoint must be able to fill in."""

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    message: str = ""


class ModelHealth(BaseModel):
    """Result of a single probe call, safe to print (never echoes the key)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    mode: str
    called_real_api: bool
    model: str
    base_url: str
    api_key_present: bool
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    reply: str = ""
    error: str | None = None
    note: str = ""


def check_model(
    config: ModelConfig,
    *,
    transport: CompletionTransport | None = None,
    replay_dir: str | None = None,
) -> ModelHealth:
    """Run one structured probe through the gateway and report what happened.

    Failures are returned as data (``ok=False`` plus ``error``) rather than
    raised: the caller is a diagnostic command, so an unreachable endpoint is an
    expected outcome, not a crash.
    """
    called_real_api = config.mode == ModelMode.LIVE and transport is None

    def result(**outcome: Any) -> ModelHealth:
        return ModelHealth(
            mode=config.mode.value,
            model=config.model,
            base_url=config.base_url,
            api_key_present=bool(config.api_key),
            called_real_api=called_real_api,
            note=_NOTES.get(config.mode, ""),
            **outcome,
        )

    if config.mode == ModelMode.LIVE and not config.api_key and transport is None:
        return result(
            ok=False,
            error=(
                "no API key configured: pass --api-key or set UPGRADELENS_MODEL_API_KEY "
                "in the environment / .env (see .env.example)"
            ),
        )

    gateway = ModelGateway(config, replay_dir=replay_dir, transport=transport)
    started = time.perf_counter()
    try:
        answer, record = gateway.complete_structured(
            prompt=PROBE_PROMPT, schema=ProbeAnswer, name="health_check"
        )
    except (
        ModelUnavailableError,
        StructuredOutputError,
        BudgetExceededError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        return result(
            ok=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )

    return result(
        ok=True,
        latency_ms=record.latency_ms or int((time.perf_counter() - started) * 1000),
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        total_tokens=record.total_tokens,
        cost_usd=record.cost_usd,
        reply=answer.message,
    )
