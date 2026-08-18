r"""Result aggregator: merge agent outcomes + surface conflicts (MA-2-3).

:class:`ResultAggregator` folds many :class:`AgentResult`\ s (e.g. the leaves of
a fan-out, or the outputs of several professional agents) into one unified
result the Workbench can render through a single template. It:

* de-duplicates :class:`Finding`\ s by ``finding_id`` (later, higher-severity
  wins, so a breaking-change agent can override a dependency-upgrade guess);
* concatenates action proposals, test results and degradations;
* merges coverage and costs (summing :class:`CostUsage`);
* flags **conflicts** -- the same finding id reported at two different
  severities -- so the supervisor can ask for human input instead of silently
  picking one;
* preserves every child trace so the UI can expand the run tree.
"""

from __future__ import annotations

from typing import Any

from upgradelens.agent.runtime import (
    AgentResult,
    AgentRunContext,
    CostUsage,
    RunStatus,
)
from upgradelens.core.finding import Finding, Severity


class ResultAggregator:
    """Merge a set of agent results into one capability-agnostic result."""

    def aggregate(
        self,
        parent_ctx: AgentRunContext,
        results: list[AgentResult],
        *,
        summary: str = "",
    ) -> AgentResult:
        findings: dict[str, Finding] = {}
        conflicts: list[dict[str, Any]] = []
        action_proposals: list[dict[str, Any]] = []
        test_results: list[dict[str, Any]] = []
        degradations: list[str] = []
        coverage: dict[str, Any] = {}
        cost = CostUsage()
        trace: list[dict[str, Any]] = []
        failed = 0

        for res in results:
            if res.status is not RunStatus.COMPLETED:
                failed += 1
            for f in res.findings:
                self._merge_finding(findings, conflicts, f)
            action_proposals.extend(res.action_proposals)
            test_results.extend(res.test_results)
            degradations.extend(res.degradations)
            coverage.update(res.coverage)
            cost = cost.merge(res.cost)
            trace.extend(res.trace)

        status = RunStatus.FAILED if failed else RunStatus.COMPLETED
        merged = AgentResult(
            run_id=parent_ctx.run_id,
            parent_run_id=parent_ctx.parent_run_id,
            agent_id=parent_ctx.agent.agent_id,
            kind=parent_ctx.agent.kind,
            status=status,
            summary=summary or _default_summary(results, failed, len(conflicts)),
            findings=list(findings.values()),
            action_proposals=action_proposals,
            coverage=coverage,
            test_results=test_results,
            degradations=degradations,
            trace=trace,
            cost=cost,
            notes={"conflicts": conflicts},
        )
        return merged

    # -- internals -------------------------------------------------------- #

    def _merge_finding(
        self,
        findings: dict[str, Finding],
        conflicts: list[dict[str, Any]],
        incoming: Finding,
    ) -> None:
        existing = findings.get(incoming.finding_id)
        if existing is None:
            findings[incoming.finding_id] = incoming
            return
        if existing.severity == incoming.severity:
            # same id, same severity -> keep the one with higher confidence
            if incoming.confidence > existing.confidence:
                findings[incoming.finding_id] = incoming
            return
        # different severity on the same id -> conflict
        conflicts.append(
            {
                "finding_id": incoming.finding_id,
                "severities": sorted(
                    {existing.severity.value, incoming.severity.value},
                    key=lambda s: _sev_rank(s),
                ),
            }
        )
        keep_existing = _sev_rank(existing.severity) >= _sev_rank(incoming.severity)
        winner = existing if keep_existing else incoming
        findings[incoming.finding_id] = winner


def _sev_rank(sev: Severity | str) -> int:
    order = {
        Severity.CRITICAL.value: 4,
        Severity.HIGH.value: 3,
        Severity.MEDIUM.value: 2,
        Severity.LOW.value: 1,
        Severity.INFO.value: 0,
    }
    key = sev.value if isinstance(sev, Severity) else str(sev)
    return order.get(key, 0)


def _default_summary(results: list[AgentResult], failed: int, conflict_count: int) -> str:
    total = len(results)
    if failed:
        return f"{total} agents ran; {failed} failed; {conflict_count} conflicts"
    return f"{total} agents ran successfully; {conflict_count} conflicts"


__all__ = ["ResultAggregator"]
