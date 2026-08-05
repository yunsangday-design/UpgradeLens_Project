"""Tests for the stage 5 model gateway (offline, deterministic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upgradelens.llm.gateway import (
    BudgetExceededError,
    CompletionRecord,
    ModelConfig,
    ModelGateway,
    ModelMode,
    ModelUnavailableError,
)
from upgradelens.models.impact import Plan, PlanItem


class _FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, schema: type) -> tuple[object, CompletionRecord]:
        self.calls += 1
        raise ModelUnavailableError("transport down")


class _OkTransport:
    def complete(self, prompt: str, schema: type) -> tuple[object, CompletionRecord]:
        obj = schema()
        rec = CompletionRecord(
            mode="live",
            model="test-model",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=1,
            cost_usd=0.0,
            cached=False,
        )
        return obj, rec


def test_fake_returns_mapped_response() -> None:
    plan = Plan(items=[PlanItem(pattern_id="validator", question="q")])
    gw = ModelGateway(ModelConfig(mode=ModelMode.FAKE), fake_responses={"planner": plan})
    obj, rec = gw.complete_structured(prompt="p", schema=Plan, name="planner")
    assert obj == plan
    assert rec.mode == "fake"
    assert len(gw.ledger) == 1


def test_fake_empty_when_unmapped() -> None:
    gw = ModelGateway(ModelConfig(mode=ModelMode.FAKE))
    obj, rec = gw.complete_structured(prompt="p", schema=Plan, name="missing")
    assert isinstance(obj, Plan)
    assert obj.items == []


def test_replay_loads_recording(tmp_path: Path) -> None:
    d = tmp_path / "replay"
    d.mkdir()
    (d / "planner.json").write_text(
        json.dumps({"output": {"items": [{"pattern_id": "validator", "question": "q"}]}}),
        encoding="utf-8",
    )
    gw = ModelGateway(ModelConfig(mode=ModelMode.REPLAY), replay_dir=str(d))
    obj, rec = gw.complete_structured(prompt="p", schema=Plan, name="planner")
    assert obj.items[0].pattern_id == "validator"
    assert rec.cached is True


def test_budget_exceeded_before_call() -> None:
    gw = ModelGateway(ModelConfig(mode=ModelMode.FAKE, max_total_tokens=5))
    with pytest.raises(BudgetExceededError):
        gw.complete_structured(prompt="x" * 1000, schema=Plan, name="planner")
    assert gw.ledger == []


def test_live_transport_success() -> None:
    gw = ModelGateway(ModelConfig(mode=ModelMode.LIVE), transport=_OkTransport())
    obj, rec = gw.complete_structured(prompt="p", schema=Plan, name="planner")
    assert isinstance(obj, Plan)
    assert rec.mode == "live"
    assert rec.total_tokens == 2


def test_live_retries_then_unavailable() -> None:
    transport = _FailingTransport()
    cfg = ModelConfig(mode=ModelMode.LIVE, max_retries=2)
    gw = ModelGateway(cfg, transport=transport)
    with pytest.raises(ModelUnavailableError):
        gw.complete_structured(prompt="p", schema=Plan, name="planner")
    # initial attempt + max_retries retries
    assert transport.calls == cfg.max_retries + 1


def test_live_without_api_key_is_unavailable() -> None:
    gw = ModelGateway(ModelConfig(mode=ModelMode.LIVE))
    with pytest.raises(ModelUnavailableError):
        gw.complete_structured(prompt="p", schema=Plan, name="planner")
