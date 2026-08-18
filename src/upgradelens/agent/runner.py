r"""Agent runner: single-agent execution, lifecycle events, parent/child runs (MA-1B-2).

:class:`AgentRunner` is the uniform engine the supervisor drives. It:

* resolves an :class:`AgentSpec` from the registry,
* emits :class:`LifecycleEvent`\ s (start / finish / error) for observability,
* records the run's :class:`CostUsage` into the shared :class:`BudgetLedger`
  (passed via :class:`AgentRunContext`),
* supports spawning **child runs** for fan-out, threading ``parent_run_id`` so
  the trace tree stays connected,
* never lets a single agent crash the plan -- failures become a ``FAILED``
  :class:`AgentResult`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from upgradelens.agent.runtime import (
    AgentKind,
    AgentResult,
    AgentRunContext,
    LifecycleEvent,
    RunId,
    RunStatus,
    TaskEnvelope,
)
from upgradelens.agent.spec import AgentRegistry, AgentSpec


class AgentRunner:
    """Execute professional agents uniformly, with lifecycle + budget hooks."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        on_event: Callable[[LifecycleEvent], None] | None = None,
    ) -> None:
        self.registry = registry
        self.on_event = on_event

    # -- public API ------------------------------------------------------- #

    def run(
        self,
        kind: AgentKind,
        ctx: AgentRunContext,
        task: TaskEnvelope,
    ) -> AgentResult:
        """Run the agent for ``kind`` under ``ctx``; failures are captured."""
        spec = self.registry.resolve(kind)
        run_ctx = self._bind_context(ctx, spec)
        self._emit(run_ctx, "start", {"task": task.kind})
        try:
            result = self._invoke(spec, run_ctx, task)
        except Exception as exc:  # noqa: BLE001 - a bad agent must not kill the plan
            result = self._failure(run_ctx, spec, exc)
        else:
            self._record_cost(run_ctx, result)
            self._emit(
                run_ctx,
                "finish",
                {"status": result.status.value, "finding_count": result.finding_count},
            )
        return result

    def run_child(
        self,
        parent_ctx: AgentRunContext,
        kind: AgentKind,
        task: TaskEnvelope,
        *,
        run_id: RunId | None = None,
    ) -> AgentResult:
        """Spawn a child run of ``kind`` beneath ``parent_ctx`` (fan-out)."""
        spec = self.registry.resolve(kind)
        child_ctx = parent_ctx.child(spec.identity(), run_id=run_id)
        return self.run(kind, child_ctx, task)

    # -- internals -------------------------------------------------------- #

    def _bind_context(self, ctx: AgentRunContext, spec: AgentSpec) -> AgentRunContext:
        if ctx.agent.kind is spec.kind and ctx.agent.agent_id == spec.agent_id:
            return ctx
        # Re-bind to the resolved agent identity while keeping budget/gateway/store.
        return AgentRunContext(
            run_id=ctx.run_id,
            agent=spec.identity(),
            parent_run_id=ctx.parent_run_id,
            mode=ctx.mode,
            permissions=ctx.permissions,
            locale=ctx.locale,
            trace_enabled=ctx.trace_enabled,
            budget=ctx.budget,
            gateway=ctx.gateway,
            store=ctx.store,
        )

    def _invoke(
        self, spec: AgentSpec, ctx: AgentRunContext, task: TaskEnvelope
    ) -> AgentResult:
        assert spec.run is not None
        return spec.run(ctx, task)

    def _record_cost(self, ctx: AgentRunContext, result: AgentResult) -> None:
        if ctx.budget is not None and hasattr(ctx.budget, "record"):
            try:
                ctx.budget.record(result.cost)
            except Exception:  # noqa: BLE001 - budget breach must not mask the result
                pass

    def _failure(
        self, ctx: AgentRunContext, spec: AgentSpec, exc: Exception
    ) -> AgentResult:
        self._emit(ctx, "error", {"error": str(exc)})
        return AgentResult(
            run_id=ctx.run_id,
            parent_run_id=ctx.parent_run_id,
            agent_id=spec.agent_id,
            kind=spec.kind,
            status=RunStatus.FAILED,
            summary=f"{spec.agent_id} failed: {exc}",
            findings=[],
            notes={"error": str(exc)},
        )

    def _emit(
        self, ctx: AgentRunContext, event: str, data: dict[str, Any]
    ) -> None:
        if self.on_event is None:
            return
        self.on_event(
            LifecycleEvent(
                run_id=ctx.run_id,
                event=event,
                parent_run_id=ctx.parent_run_id,
                data=data,
            )
        )


__all__ = ["AgentRunner"]
