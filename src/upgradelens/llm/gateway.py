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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel

_M = TypeVar("_M", bound=BaseModel)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


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

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    @property
    def ledger(self) -> list[CompletionRecord]:
        return list(self._ledger)

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
            raise ModelUnavailableError("no api_key configured for live mode")

        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        llm = ChatOpenAI(
            model=cfg.model,
            base_url=cfg.base_url or None,
            api_key=SecretStr(cfg.api_key) if cfg.api_key else None,
            temperature=cfg.temperature,
            timeout=cfg.request_timeout_seconds,
            max_retries=cfg.max_retries,
        )
        try:
            out = llm.with_structured_output(schema).invoke(prompt)
        except ModelUnavailableError:
            raise
        except Exception as exc:  # network / timeout / auth / validation
            raise ModelUnavailableError(str(exc)) from exc

        typed_out = cast(_M, out)
        text = typed_out.model_dump_json()
        pt = estimate_tokens(prompt)
        ct = estimate_tokens(text)
        cost = pt / 1000.0 * cfg.price_per_1k_input_usd + ct / 1000.0 * cfg.price_per_1k_output_usd
        rec = CompletionRecord(
            mode="live",
            model=cfg.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=pt + ct,
            latency_ms=0,
            cost_usd=round(cost, 6),
            cached=False,
            error=None,
        )
        return typed_out, rec

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
