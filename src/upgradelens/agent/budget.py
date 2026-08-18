"""Shared budget ledger + runtime policy (MA-1A-2).

Every agent run draws from one :class:`BudgetLedger` owned by the supervisor and
passed down via :class:`~upgradelens.agent.runtime.AgentRunContext`. Recording a
:class:`~upgradelens.agent.runtime.CostUsage` merges into the running total and,
depending on the :class:`BudgetPolicy`, either warns or refuses once a cap is
hit. This gives the supervisor a single, capability-agnostic way to bound cost,
latency and tool usage across a whole multi-agent plan.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from upgradelens.agent.runtime import CostUsage, RunId

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class BudgetPolicy(StrEnum):
    """What to do when a budget cap is breached."""

    WARN = "warn"  # record anyway, emit a warning
    FAIL = "fail"  # raise BudgetExhausted


class BudgetExhausted(Exception):
    """Raised when a :class:`BudgetPolicy.FAIL` ledger goes over a cap."""


# ---------------------------------------------------------------------------
# Budget specification
# ---------------------------------------------------------------------------


@dataclass
class BudgetSpec:
    """The resource caps a run must respect.

    All fields are optional; ``None`` means "no limit" on that dimension.
    """

    max_total_tokens: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    max_latency_ms: float | None = None
    max_turns: int | None = None


def default_budget_spec(max_total_tokens: int | None = None) -> BudgetSpec:
    """Build a budget from the existing CLI/agent ``budget_tokens`` knob."""
    return BudgetSpec(max_total_tokens=max_total_tokens)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class BudgetLedger:
    """A monotonic tally of resource usage against a :class:`BudgetSpec`."""

    spec: BudgetSpec = field(default_factory=BudgetSpec)
    policy: BudgetPolicy = BudgetPolicy.WARN
    run_id: RunId | None = None
    total: CostUsage = field(default_factory=CostUsage)
    entries: list[CostUsage] = field(default_factory=list)
    # parallel-wave agents record concurrently; the lock keeps totals exact
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # -- recording -------------------------------------------------------- #

    def record(self, cost: CostUsage) -> CostUsage:
        """Merge ``cost`` into the running total, enforcing the policy.

        Thread-safe: concurrent agents in one parallel wave may record at the
        same time. Returns the new running total. Raises :class:`BudgetExhausted`
        on a :class:`BudgetPolicy.FAIL` breach.
        """
        with self._lock:
            self.total = self.total.merge(cost)
            self.entries.append(cost)
            breach = self._first_breach()
            if breach is None:
                return self.total
            if self.policy is BudgetPolicy.FAIL:
                cap = getattr(self.spec, breach)
                raise BudgetExhausted(
                    f"budget exhausted on {breach}: "
                    f"{self._usage_value(breach)} >= {cap}"
                )
            return self.total

    def record_legacy_entry(self, entry: dict[str, Any], *, model: str = "") -> CostUsage:
        """Record a cost from an existing loop :class:`Ledger` entry dict."""
        return self.record(CostUsage.from_ledger_entry(entry, model=model))

    # -- queries ---------------------------------------------------------- #

    def remaining_tokens(self) -> int | None:
        if self.spec.max_total_tokens is None:
            return None
        used = self.total.input_tokens + self.total.output_tokens
        return max(0, self.spec.max_total_tokens - used)

    def remaining_cost_usd(self) -> float | None:
        if self.spec.max_cost_usd is None:
            return None
        return max(0.0, self.spec.max_cost_usd - self.total.cost_usd)

    def is_exhausted(self) -> bool:
        return self._first_breach() is not None

    def breached_dimensions(self) -> list[str]:
        out: list[str] = []
        if (
            self.spec.max_total_tokens is not None
            and self.total.total >= self.spec.max_total_tokens
        ):
            out.append("max_total_tokens")
        if (
            self.spec.max_tool_calls is not None
            and self.total.tool_calls >= self.spec.max_tool_calls
        ):
            out.append("max_tool_calls")
        if self.spec.max_cost_usd is not None and self.total.cost_usd >= self.spec.max_cost_usd:
            out.append("max_cost_usd")
        if (
            self.spec.max_latency_ms is not None
            and self.total.latency_ms >= self.spec.max_latency_ms
        ):
            out.append("max_latency_ms")
        return out

    def _usage_value(self, dimension: str) -> float:
        """The running usage for a budget dimension (mirrors ``breached_dimensions``)."""
        if dimension == "max_total_tokens":
            return float(self.total.total)
        if dimension == "max_tool_calls":
            return float(self.total.tool_calls)
        if dimension == "max_cost_usd":
            return self.total.cost_usd
        if dimension == "max_latency_ms":
            return self.total.latency_ms
        raise KeyError(dimension)

    def report(self) -> dict[str, Any]:
        """A compact, UI-friendly snapshot of usage vs. caps."""
        return {
            "total": self.total.model_dump(mode="json"),
            "spec": {
                "max_total_tokens": self.spec.max_total_tokens,
                "max_tool_calls": self.spec.max_tool_calls,
                "max_cost_usd": self.spec.max_cost_usd,
                "max_latency_ms": self.spec.max_latency_ms,
                "max_turns": self.spec.max_turns,
            },
            "remaining_tokens": self.remaining_tokens(),
            "remaining_cost_usd": self.remaining_cost_usd(),
            "breached": self.breached_dimensions(),
        }

    # -- internals -------------------------------------------------------- #

    def _first_breach(self) -> str | None:
        breaches = self.breached_dimensions()
        return breaches[0] if breaches else None


__all__ = [
    "BudgetPolicy",
    "BudgetExhausted",
    "BudgetSpec",
    "default_budget_spec",
    "BudgetLedger",
]
