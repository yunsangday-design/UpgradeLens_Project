from __future__ import annotations

from upgradelens.llm.gateway import (
    CompletionRecord,
    ModelConfig,
    ModelGateway,
    ModelMode,
)
from upgradelens.llm.query_rewrite import QueryRewrites, rewrite_query


class _FakeTransport:
    """Routes a LIVE completion straight to a canned response (test only)."""

    def __init__(self, response: QueryRewrites) -> None:
        self._response = response

    def complete(self, prompt: str, schema):
        record = CompletionRecord(
            mode="live",
            model="stub",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=0,
            cost_usd=0.0,
            cached=False,
        )
        return self._response, record


def test_fake_returns_single_deterministic_query() -> None:
    out = rewrite_query(
        None,
        package="pydantic",
        source_version="",
        target_version="2.0",
        user_intent="",
        code_symbols=["validator"],
        mode=ModelMode.FAKE,
    )
    assert len(out) == 1
    assert "validator" in out[0]


def test_live_uses_llm_rewrite() -> None:
    gateway = ModelGateway(
        config=ModelConfig(mode=ModelMode.LIVE),
        transport=_FakeTransport(
            QueryRewrites(queries=["pydantic v2 validator migration", "pydantic validator removed"])
        ),
    )
    out = rewrite_query(
        gateway,
        package="pydantic",
        source_version="",
        target_version="2.0",
        user_intent="",
        code_symbols=["validator"],
        mode=ModelMode.LIVE,
    )
    assert out == ["pydantic v2 validator migration", "pydantic validator removed"]


def test_live_falls_back_when_backend_unavailable() -> None:
    # LIVE mode but no transport/api_key -> ModelUnavailableError -> deterministic fallback.
    gateway = ModelGateway(config=ModelConfig(mode=ModelMode.LIVE))
    out = rewrite_query(
        gateway,
        package="pydantic",
        source_version="",
        target_version="2.0",
        user_intent="",
        code_symbols=["validator"],
        mode=ModelMode.LIVE,
    )
    assert len(out) == 1
    assert "validator" in out[0]
