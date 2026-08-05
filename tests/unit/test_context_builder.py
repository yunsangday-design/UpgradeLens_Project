"""Tests for the stage 5 context builder (bounded, repo-free)."""

from __future__ import annotations

from upgradelens.llm.context import ContextBuilder, build_context, estimate_tokens
from upgradelens.llm.gateway import TokenBudget
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, Plan, PlanItem


def _bundle(n: int) -> EvidenceBundle:
    b = EvidenceBundle()
    for i in range(n):
        b.add(
            EvidenceItem(
                evidence_id=f"e{i}",
                kind="code_usage",
                summary=f"summary {i}",
                detail="x" * 500,
            )
        )
    return b


def test_details_are_truncated_not_full() -> None:
    ctx = build_context(_bundle(3), None, max_context_tokens=1_000_000)
    assert "x" * 500 not in ctx  # detail capped at 400 chars
    assert "summary 0" in ctx


def test_truncation_within_token_budget() -> None:
    ctx = build_context(_bundle(60), None, max_context_tokens=200)
    assert "truncated" in ctx


def test_budget_object_blocks_context() -> None:
    budget = TokenBudget(50)
    ctx = build_context(_bundle(80), None, budget=budget, max_context_tokens=10_000_000)
    assert "truncated" in ctx
    assert budget.used_tokens <= 50


def test_plan_block_is_included() -> None:
    plan = Plan(items=[PlanItem(pattern_id="validator", question="still works?")])
    ctx = build_context(_bundle(1), plan, max_context_tokens=1_000_000)
    assert "validator" in ctx


def test_estimate_tokens_monotonic() -> None:
    assert estimate_tokens("a") <= estimate_tokens("aaaaaa")


def test_context_builder_wrapper() -> None:
    cb = ContextBuilder()
    out = cb.build(_bundle(2), None, max_context_tokens=1_000_000)
    assert "summary 0" in out
