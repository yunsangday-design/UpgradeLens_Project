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
)

__all__ = [
    "BudgetExceededError",
    "CompletionRecord",
    "CompletionTransport",
    "ContextBuilder",
    "FakeBackend",
    "ModelConfig",
    "ModelGateway",
    "ModelMode",
    "ModelUnavailableError",
    "ReplayBackend",
    "StructuredOutputError",
    "TokenBudget",
    "estimate_tokens",
]
