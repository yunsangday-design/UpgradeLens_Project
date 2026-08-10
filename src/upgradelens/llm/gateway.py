"""Model gateway: a single entry point for structured completion.

Three modes are supported:

* ``fake``    - returns canned responses registered per node name; offline, deterministic.
* ``replay``  - replays recorded responses from a directory; offline.
* ``live``    - calls an OpenAI-compatible endpoint (e.g. 阿里云百炼) with structured output.

The gateway enforces a token budget before every call and records token/latency/
cost usage so the loop can be audited. Live calls go through a pluggable
:class:`CompletionTransport` so transient failures, retries and timeouts can be
exercised offline in tests.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel

_M = TypeVar("_M", bound=BaseModel)

#: Structured-output strategies tried in order. ``function_calling`` is the most
#: faithful one but several OpenAI-compatible endpoints (including some 百炼
#: models) do not expose tools, in which case we fall back to plain JSON mode.
_STRUCTURED_METHODS: tuple[str, ...] = ("function_calling", "json_mode")

#: Substrings identifying a *capability* failure -- the endpoint will never accept
#: this strategy, so retrying it on the next node only burns a round trip. Thinking
#: models on 百炼 reject ``tool_choice=required``, which is exactly how LangChain
#: pins a structured-output tool.
_CAPABILITY_FAILURE_MARKERS: tuple[str, ...] = (
    "tool_choice",
    "does not support tools",
    "tools is not supported",
    "function calling is not supported",
    "unsupported parameter",
)

#: How many times to re-ask after the model returned JSON that failed validation.
#: One repair round is enough for the common case (a missing required field);
#: more would multiply latency on an endpoint that is already slow.
_JSON_REPAIR_ATTEMPTS = 1


def is_capability_failure(error: str) -> bool:
    """Whether ``error`` means the strategy itself is unsupported by the endpoint."""
    lowered = error.lower()
    return any(marker in lowered for marker in _CAPABILITY_FAILURE_MARKERS)


def repair_prompt(prompt: str, error: str) -> str:
    """Re-ask after a schema violation, quoting the validation error verbatim.

    The model returned syntactically valid JSON that did not satisfy the schema
    (typically an empty object). Echoing the concrete error is far more effective
    than repeating the schema it already ignored once.
    """
    return (
        f"{prompt}\n\n"
        "Your previous answer was rejected by schema validation:\n"
        f"{error}\n\n"
        "Return a corrected JSON object. Fill every required field with a real "
        "value derived from the provided evidence; never return an empty object."
    )


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def usage_from_message(raw: Any) -> tuple[int | None, int | None]:
    """Extract real (prompt, completion) token counts from a provider response.

    Returns ``(None, None)`` when the provider does not report usage, so callers
    can fall back to :func:`estimate_tokens` instead of reporting zeros.
    """
    usage = getattr(raw, "usage_metadata", None)
    if isinstance(usage, dict):
        prompt = usage.get("input_tokens")
        completion = usage.get("output_tokens")
        if isinstance(prompt, int) or isinstance(completion, int):
            return (
                prompt if isinstance(prompt, int) else None,
                completion if isinstance(completion, int) else None,
            )
    metadata = getattr(raw, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage") or metadata.get("usage")
        if isinstance(token_usage, dict):
            prompt = token_usage.get("prompt_tokens")
            completion = token_usage.get("completion_tokens")
            return (
                prompt if isinstance(prompt, int) else None,
                completion if isinstance(completion, int) else None,
            )
    return None, None


def json_mode_prompt(prompt: str, schema: type[BaseModel]) -> str:
    """Append an explicit JSON contract for endpoints without tool support."""
    contract = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        f"{prompt}\n\n"
        "Respond with a single JSON object only (no prose, no markdown fences) "
        f"matching this JSON Schema:\n{contract}"
    )


class ModelMode(StrEnum):
    FAKE = "fake"
    REPLAY = "replay"
    LIVE = "live"


@dataclass(frozen=True)
class CompletionRecord:
    mode: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    cost_usd: float
    cached: bool
    error: str | None = None


@dataclass
class ModelConfig:
    mode: ModelMode = ModelMode.FAKE
    base_url: str = ""
    model: str = "qwen-plus"
    api_key: str = ""
    temperature: float = 0.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 2
    max_tokens: int | None = None
    max_total_tokens: int = 20000
    price_per_1k_input_usd: float = 0.0
    price_per_1k_output_usd: float = 0.0
    # Reasoning models (e.g. qwen-plus-thinking / qwen3.7-plus) reject
    # ``tool_choice`` and spend a long time "thinking", which makes both
    # function_calling and json_mode unreliable. Disabling thinking restores
    # function_calling and removes the timeout risk for structured extraction.
    disable_thinking: bool = False


class BudgetExceededError(RuntimeError):
    def __init__(self, *, requested: int, used: int, max_total: int) -> None:
        super().__init__(
            f"token budget exceeded: requested={requested} used={used} max={max_total}"
        )
        self.requested = requested
        self.used = used
        self.max_total = max_total


class ModelUnavailableError(RuntimeError):
    """Raised when the live model cannot be reached or returns no structured output."""


class StructuredOutputError(RuntimeError):
    """Raised when the live model returns payloads that fail schema validation."""


class TokenBudget:
    def __init__(self, max_total_tokens: int) -> None:
        self._max = max_total_tokens
        self._used = 0

    @property
    def max_total_tokens(self) -> int:
        return self._max

    @property
    def used_tokens(self) -> int:
        return self._used

    @property
    def remaining_tokens(self) -> int:
        return max(0, self._max - self._used)

    def consume(self, tokens: int) -> None:
        projected = self._used + max(0, int(tokens))
        if projected > self._max:
            raise BudgetExceededError(requested=int(tokens), used=self._used, max_total=self._max)
        self._used = projected


@runtime_checkable
class CompletionTransport(Protocol):
    def complete(self, prompt: str, schema: type[_M]) -> tuple[_M, CompletionRecord]: ...


class FakeBackend:
    def __init__(self, responses: dict[str, BaseModel]) -> None:
        self._responses = responses

    def complete(self, prompt: str, schema: type[_M], name: str) -> tuple[_M, CompletionRecord]:
        obj: BaseModel = self._responses.get(name) or schema()
        text = obj.model_dump_json()
        pt = estimate_tokens(prompt)
        ct = estimate_tokens(text)
        rec = CompletionRecord(
            mode="fake",
            model="fake",
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=pt + ct,
            latency_ms=0,
            cost_usd=0.0,
            cached=False,
            error=None,
        )
        return obj, rec  # type: ignore[return-value]


class ReplayBackend:
    def __init__(self, replay_dir: str) -> None:
        self._dir = replay_dir

    def complete(self, prompt: str, schema: type[_M], name: str) -> tuple[_M, CompletionRecord]:
        path = os.path.join(self._dir, f"{name}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"replay recording missing: {path}")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        obj = schema.model_validate(data["output"])
        text = json.dumps(data["output"], ensure_ascii=False)
        pt = estimate_tokens(prompt)
        ct = estimate_tokens(text)
        rec = CompletionRecord(
            mode="replay",
            model="replay",
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=pt + ct,
            latency_ms=0,
            cost_usd=0.0,
            cached=True,
            error=None,
        )
        return obj, rec


class ModelGateway:
    def __init__(
        self,
        config: ModelConfig,
        *,
        fake_responses: dict[str, BaseModel] | None = None,
        replay_dir: str | None = None,
        recording_dir: str | None = None,
        transport: CompletionTransport | None = None,
    ) -> None:
        self._config = config
        self._budget = TokenBudget(config.max_total_tokens)
        self._ledger: list[CompletionRecord] = []
        self._fake = FakeBackend(fake_responses or {})
        self._replay = ReplayBackend(replay_dir) if replay_dir else None
        self._recording_dir = recording_dir
        self._transport = transport
        self._disable_thinking = config.disable_thinking
        # Strategies the endpoint has already rejected as unsupported. Cached for
        # the lifetime of the run so later nodes skip a doomed round trip.
        self._unsupported_methods: set[str] = set()

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    @property
    def ledger(self) -> list[CompletionRecord]:
        return list(self._ledger)

    @property
    def mode(self) -> ModelMode:
        """The active run mode -- handy for callers that must skip the model."""
        return self._config.mode

    def complete_structured(
        self, *, prompt: str, schema: type[_M], name: str
    ) -> tuple[_M, CompletionRecord]:
        prompt_tokens = estimate_tokens(prompt)
        self._budget.consume(prompt_tokens)

        mode = self._config.mode
        if mode == ModelMode.FAKE:
            obj, rec = self._fake.complete(prompt, schema, name)
        elif mode == ModelMode.REPLAY:
            if self._replay is None:
                raise ValueError("replay mode requires replay_dir")
            obj, rec = self._replay.complete(prompt, schema, name)
        else:
            obj, rec = self._live_complete(prompt, schema)

        self._ledger.append(rec)
        self._budget.consume(rec.completion_tokens)
        rec_dir = self._recording_dir
        if rec_dir is not None and mode != ModelMode.REPLAY:
            self._write_recording(name, obj, rec_dir)
        return obj, rec

    def _write_recording(self, name: str, obj: BaseModel, recording_dir: str) -> None:
        """Persist a node response so it can be replayed offline later.

        The file name matches the node name passed to :meth:`complete_structured`
        (e.g. ``planner``, ``extractor__<pattern_id>``, ``impact_analyzer``), which
        is exactly what :class:`ReplayBackend` looks up.
        """
        out_dir = Path(recording_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {"output": obj.model_dump(mode="json")}
        with open(out_dir / f"{name}.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def _live_complete(self, prompt: str, schema: type[_M]) -> tuple[_M, CompletionRecord]:
        cfg = self._config
        if self._transport is not None:
            raw = self._with_retry(lambda: self._transport.complete(prompt, schema))
            return cast("tuple[_M, CompletionRecord]", raw)

        if not cfg.api_key:
            raise ModelUnavailableError(
                "live mode needs an API key: pass --api-key or set "
                "UPGRADELENS_MODEL_API_KEY (see .env.example)"
            )

        llm = self._build_chat_model()
        started = time.perf_counter()
        typed_out, reported = self._invoke_structured(llm, prompt, schema)
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = typed_out.model_dump_json()
        reported_prompt, reported_completion = reported
        pt = reported_prompt if reported_prompt is not None else estimate_tokens(prompt)
        ct = reported_completion if reported_completion is not None else estimate_tokens(text)
        cost = pt / 1000.0 * cfg.price_per_1k_input_usd + ct / 1000.0 * cfg.price_per_1k_output_usd
        rec = CompletionRecord(
            mode="live",
            model=cfg.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=pt + ct,
            latency_ms=latency_ms,
            cost_usd=round(cost, 6),
            cached=False,
            error=None,
        )
        return typed_out, rec

    def _build_chat_model(self) -> Any:
        cfg = self._config
        try:
            from langchain_openai import ChatOpenAI
            from pydantic import SecretStr
        except ImportError as exc:  # pragma: no cover - dependency always installed
            raise ModelUnavailableError(
                "langchain-openai is required for live mode; run `uv sync`"
            ) from exc

        kwargs: dict[str, Any] = {}
        if self._disable_thinking:
            # Reasoning models (qwen3.7-plus etc.) reject tool_choice in thinking
            # mode. Disabling thinking restores function_calling and avoids the
            # long "thinking" latency that otherwise times out structured calls.
            kwargs["extra_body"] = {"enable_thinking": False}
        return ChatOpenAI(
            model=cfg.model,
            base_url=cfg.base_url or None,
            api_key=SecretStr(cfg.api_key) if cfg.api_key else None,
            temperature=cfg.temperature,
            timeout=cfg.request_timeout_seconds,
            max_retries=cfg.max_retries,
            **kwargs,
        )

    def _invoke_structured(
        self, llm: Any, prompt: str, schema: type[_M]
    ) -> tuple[_M, tuple[int | None, int | None]]:
        """Call the endpoint, trying each structured-output strategy in turn.

        Two endpoint realities shape this loop:

        * A strategy the endpoint cannot support (thinking models reject the
          ``tool_choice`` LangChain uses to pin a tool) fails identically every
          time, so it is remembered and skipped for the rest of the run.
        * A strategy that *is* supported can still return JSON violating the
          schema, which is usually fixed by re-asking once with the validation
          error quoted back.

        Every remaining failure surfaces as :class:`ModelUnavailableError` so the
        graph can fall back to the static report instead of crashing.
        """
        failures: list[str] = []
        for method in _STRUCTURED_METHODS:
            if method in self._unsupported_methods:
                continue
            try:
                runnable = llm.with_structured_output(schema, method=method, include_raw=True)
            except Exception as exc:  # pragma: no cover - construction rarely fails
                failures.append(f"{method}: {exc}")
                continue

            result = self._try_method(runnable, method, prompt, schema, failures)
            if result is not None:
                return result

        raise ModelUnavailableError(
            f"structured output failed for model {self._config.model}: " + " | ".join(failures)
        )

    def _try_method(
        self,
        runnable: Any,
        method: str,
        prompt: str,
        schema: type[_M],
        failures: list[str],
    ) -> tuple[_M, tuple[int | None, int | None]] | None:
        """Run one strategy, repairing schema violations; ``None`` means give up."""
        text = prompt if method == "function_calling" else json_mode_prompt(prompt, schema)

        for attempt in range(_JSON_REPAIR_ATTEMPTS + 1):
            try:
                payload = runnable.invoke(text)
            except Exception as exc:  # network / timeout / auth / unsupported method
                failures.append(f"{method}: {exc}")
                if is_capability_failure(str(exc)):
                    self._unsupported_methods.add(method)
                return None

            if not isinstance(payload, dict):
                return cast(_M, payload), (None, None)
            parsed = payload.get("parsed")
            if parsed is not None:
                return cast(_M, parsed), usage_from_message(payload.get("raw"))

            reason = str(payload.get("parsing_error") or "model returned no structured output")
            failures.append(f"{method} attempt {attempt + 1}: {reason}")
            # Only json_mode can be repaired by re-prompting; a tool call that
            # produced nothing will not improve from restating the schema.
            if method != "json_mode":
                return None
            text = repair_prompt(json_mode_prompt(prompt, schema), reason)
        return None

    def _with_retry(self, fn: Any) -> Any:
        attempts = 0
        last: Exception | None = None
        while attempts <= self._config.max_retries:
            attempts += 1
            try:
                return fn()
            except ModelUnavailableError as exc:
                last = exc
            except Exception as exc:  # any transport failure is treated as unavailable
                last = ModelUnavailableError(str(exc))
        raise last if last is not None else ModelUnavailableError("retries exhausted")
