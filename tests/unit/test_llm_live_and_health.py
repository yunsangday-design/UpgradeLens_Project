"""Tests for the live-mode plumbing and the ``llm-check`` self-check.

The live path cannot be exercised against a real endpoint in CI, so the pieces
that decide whether a real call succeeds -- structured-output negotiation and
usage extraction -- are tested directly with a stand-in chat model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from upgradelens.cli import EXIT_INVALID_REQUEST, EXIT_OK, EXIT_RUNTIME, main
from upgradelens.llm.gateway import (
    CompletionRecord,
    ModelConfig,
    ModelGateway,
    ModelMode,
    ModelUnavailableError,
    json_mode_prompt,
    usage_from_message,
)
from upgradelens.llm.health import ProbeAnswer, check_model


class _Message:
    def __init__(self, usage: dict[str, Any] | None = None, metadata: Any = None) -> None:
        self.usage_metadata = usage
        self.response_metadata = metadata


class _Runnable:
    def __init__(self, owner: _FakeChatModel, method: str) -> None:
        self._owner = owner
        self._method = method

    def invoke(self, prompt: str) -> dict[str, Any]:
        self._owner.prompts.append((self._method, prompt))
        if self._method in self._owner.unparsable:
            return {"parsed": None, "raw": _Message(), "parsing_error": "not json"}
        return {
            "parsed": ProbeAnswer(ok=True, message="pong"),
            "raw": _Message(usage={"input_tokens": 11, "output_tokens": 7}),
            "parsing_error": None,
        }


class _FakeChatModel:
    """Stand-in for ``ChatOpenAI`` covering endpoint capability differences."""

    def __init__(self, unsupported: tuple[str, ...] = (), unparsable: tuple[str, ...] = ()) -> None:
        self.unsupported = unsupported
        self.unparsable = unparsable
        self.prompts: list[tuple[str, str]] = []

    def with_structured_output(
        self, schema: type, *, method: str = "function_calling", include_raw: bool = False
    ) -> _Runnable:
        if method in self.unsupported:
            raise RuntimeError(f"{method} is not supported by this endpoint")
        return _Runnable(self, method)


def _gateway(**kwargs: Any) -> ModelGateway:
    return ModelGateway(ModelConfig(mode=ModelMode.LIVE, api_key="k", **kwargs))


def test_usage_from_message_prefers_provider_counts() -> None:
    assert usage_from_message(_Message(usage={"input_tokens": 3, "output_tokens": 5})) == (3, 5)
    legacy = _Message(metadata={"token_usage": {"prompt_tokens": 8, "completion_tokens": 2}})
    assert usage_from_message(legacy) == (8, 2)
    assert usage_from_message(_Message()) == (None, None)


def test_json_mode_prompt_carries_the_schema() -> None:
    prompt = json_mode_prompt("analyse this", ProbeAnswer)
    assert "analyse this" in prompt
    assert "JSON Schema" in prompt
    assert "message" in prompt  # the schema itself is inlined


def test_live_call_falls_back_to_json_mode_when_tools_are_unsupported() -> None:
    llm = _FakeChatModel(unsupported=("function_calling",))
    answer, usage = _gateway()._invoke_structured(llm, "probe", ProbeAnswer)

    assert answer.message == "pong"
    assert usage == (11, 7)
    # Only the fallback attempt reached the endpoint, and it carried the schema.
    assert [method for method, _ in llm.prompts] == ["json_mode"]
    assert "JSON Schema" in llm.prompts[0][1]


def test_live_call_reports_real_token_usage() -> None:
    gateway = _gateway()
    gateway._invoke_structured = (  # type: ignore[method-assign]
        lambda llm, prompt, schema: (ProbeAnswer(ok=True, message="pong"), (11, 7))
    )
    gateway._build_chat_model = lambda: _FakeChatModel()  # type: ignore[method-assign]

    _, record = gateway.complete_structured(prompt="probe", schema=ProbeAnswer, name="n")

    assert (record.prompt_tokens, record.completion_tokens) == (11, 7)
    assert record.mode == "live"


def test_live_call_raises_when_every_strategy_fails() -> None:
    llm = _FakeChatModel(unsupported=("function_calling",), unparsable=("json_mode",))
    with pytest.raises(ModelUnavailableError) as excinfo:
        _gateway()._invoke_structured(llm, "probe", ProbeAnswer)

    message = str(excinfo.value)
    assert "function_calling" in message and "json_mode" in message


def test_disable_thinking_passes_extra_body_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _SpyChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def with_structured_output(
            self, schema: type, *, method: str = "function_calling", include_raw: bool = False
        ):
            return _Runnable(_FakeChatModel(), method)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _SpyChatOpenAI)
    gateway = _gateway(disable_thinking=True)
    gateway._build_chat_model()  # construct the client

    assert captured.get("extra_body") == {"enable_thinking": False}


def test_enable_thinking_omits_extra_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _SpyChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def with_structured_output(
            self, schema: type, *, method: str = "function_calling", include_raw: bool = False
        ):
            return _Runnable(_FakeChatModel(), method)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _SpyChatOpenAI)
    gateway = _gateway(disable_thinking=False)
    gateway._build_chat_model()

    assert "extra_body" not in captured


def test_health_check_flags_missing_api_key() -> None:
    health = check_model(ModelConfig(mode=ModelMode.LIVE, api_key=""))

    assert health.ok is False
    assert health.called_real_api is True
    assert health.error is not None and "API key" in health.error


def test_health_check_through_transport_reports_usage() -> None:
    class _Transport:
        def complete(self, prompt: str, schema: type) -> tuple[Any, CompletionRecord]:
            record = CompletionRecord(
                mode="live",
                model="qwen-plus",
                prompt_tokens=9,
                completion_tokens=4,
                total_tokens=13,
                latency_ms=42,
                cost_usd=0.0,
                cached=False,
            )
            return ProbeAnswer(ok=True, message="pong"), record

    health = check_model(ModelConfig(mode=ModelMode.LIVE, api_key="k"), transport=_Transport())

    assert health.ok is True
    assert health.reply == "pong"
    assert health.latency_ms == 42
    assert health.total_tokens == 13
    # A stubbed transport is not the real endpoint and must not claim to be.
    assert health.called_real_api is False


def test_llm_check_cli_fake_mode_is_marked_as_not_real(capsys: Any) -> None:
    rc = main(["llm-check", "--mode", "fake"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == EXIT_OK
    assert payload["ok"] is True
    assert payload["called_real_api"] is False
    assert "no real API call" in captured.err


def test_llm_check_cli_live_without_key_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)  # keep a developer .env out of the assertion
    monkeypatch.setenv("UPGRADELENS_MODEL_API_KEY", "")

    rc = main(["llm-check", "--mode", "live"])

    assert rc == EXIT_RUNTIME
    assert "model check failed" in capsys.readouterr().err


def test_llm_check_cli_replay_requires_dir() -> None:
    assert main(["llm-check", "--mode", "replay"]) == EXIT_INVALID_REQUEST
