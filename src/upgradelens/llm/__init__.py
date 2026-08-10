"""LLM gateway package (stage 5)."""

from upgradelens.llm.context import ContextBuilder
from upgradelens.llm.gateway import (
    BudgetExceededError,
    CompletionRecord,
    CompletionTransport,
    FakeBackend,
    ModelConfig,
    ModelGateway,
    ModelMode,
    ModelUnavailableError,
    ReplayBackend,
    StructuredOutputError,
    TokenBudget,
    estimate_tokens,
    usage_from_message,
)
from upgradelens.llm.health import ModelHealth, ProbeAnswer, check_model

__all__ = [
    "BudgetExceededError",
    "CompletionRecord",
    "CompletionTransport",
    "ContextBuilder",
    "FakeBackend",
    "ModelConfig",
    "ModelGateway",
    "ModelHealth",
    "ModelMode",
    "ModelUnavailableError",
    "ProbeAnswer",
    "ReplayBackend",
    "StructuredOutputError",
    "TokenBudget",
    "check_model",
    "estimate_tokens",
    "usage_from_message",
]
