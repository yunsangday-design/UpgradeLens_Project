"""Tool Trace: a tamper-evident record of every external fetch.

Stage 7's central promise is *"the LLM never touches the network; every byte
of upstream evidence comes through a traced tool"*. The trace is what makes
that auditable -- it is attached to the assessment output and, in a live run,
shows exactly which URLs were hit, how many bytes came back, and whether the
result was served from cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallEvent:
    """One recorded external interaction."""

    tool: str
    target: str
    status: str  # "ok" | "cached" | "error"
    http_status: int | None = None
    bytes: int = 0
    latency_ms: float = 0.0
    cache_hit: bool = False
    error: str | None = None
    timestamp: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "target": self.target,
            "status": self.status,
            "http_status": self.http_status,
            "bytes": self.bytes,
            "latency_ms": round(self.latency_ms, 1),
            "cache_hit": self.cache_hit,
            "error": self.error,
            "timestamp": self.timestamp,
            "params": self.params,
        }


class ToolTrace:
    """Collects :class:`ToolCallEvent` records for a single run."""

    def __init__(self) -> None:
        self.events: list[ToolCallEvent] = []

    def record(
        self,
        *,
        tool: str,
        target: str,
        status: str,
        http_status: int | None = None,
        bytes_: int = 0,
        latency_ms: float = 0.0,
        cache_hit: bool = False,
        error: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolCallEvent:
        event = ToolCallEvent(
            tool=tool,
            target=target,
            status=status,
            http_status=http_status,
            bytes=bytes_,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            error=error,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            params=params or {},
        )
        self.events.append(event)
        return event

    def to_dict(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    def network_bytes(self) -> int:
        """Total bytes actually pulled from the network (excludes cache hits)."""
        return sum(e.bytes for e in self.events if not e.cache_hit and e.status == "ok")

    def network_calls(self) -> int:
        return sum(1 for e in self.events if e.status == "ok" and not e.cache_hit)

    def cache_hits(self) -> int:
        return sum(1 for e in self.events if e.cache_hit)
