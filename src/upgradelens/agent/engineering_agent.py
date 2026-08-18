"""EngineeringAgent — the unified natural-language front door for all five capabilities.

This is the M1 / A3 consolidation layer: a single object that turns a free-text
request into a structured :class:`SoftwareTask` and runs it through the already
built controlled execution layer (``run_supervisor`` / ``dispatch_by_task``).

It does *not* re-implement any capability logic. It only:

* routes natural language -> SoftwareTask via :class:`Router` (offline rules;
  live refines and validates the GitHub URL first, spending no token on bad ones);
* lets callers override the routed context with explicit, trusted kwargs
  (repo / diff / dependency / versions);
* fans out to the five capabilities through the Supervisor + Handoff layer
  (single capability short-circuits to ``dispatch_by_task``; multiple capabilities
  run as isolated sub-agents and aggregate through one verification gate);
* returns one normalised :class:`EngineeringResult`.

Usage mirrors :class:`DependencyUpgradeAgent`::

    from upgradelens import EngineeringAgent

    agent = EngineeringAgent(mode="fake")
    result = agent.run("review the security of https://github.com/o/r")
    for finding in result.findings:
        print(finding.severity, finding.summary)
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.agent.router import Router
from upgradelens.agent.supervisor import (
    AgentContext,
    SupervisorResult,
    decompose_task,
    run_supervisor,
)
from upgradelens.capabilities.workbench import CapabilityRunResult
from upgradelens.config import Settings
from upgradelens.core.finding import Finding
from upgradelens.core.task import SoftwareTask, TaskKind
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode

logger = logging.getLogger(__name__)

__all__ = ["EngineeringAgent", "EngineeringResult"]


def _collect_findings(result: CapabilityRunResult | None) -> list[Finding]:
    """Reconstruct :class:`Finding` objects from a capability result (best effort).

    ``CapabilityRunResult.findings`` holds JSON-safe dicts (the workbench
    serialises every ``Finding``). Reconstruction can trip the pydantic validator
    when a ``verified`` finding lost its evidence ids during serialisation, so we
    degrade such entries to ``candidate`` and otherwise skip them rather than
    failing the whole aggregate.
    """
    if result is None:
        return []
    out: list[Finding] = []
    for item in result.findings or []:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Finding.model_validate(item))
        except Exception:
            try:
                relaxed = dict(item)
                if (
                    relaxed.get("status") == "verified"
                    and not relaxed.get("evidence_ids")
                ):
                    relaxed["status"] = "candidate"
                out.append(Finding.model_validate(relaxed))
            except Exception:
                continue
    return out


class EngineeringResult(BaseModel):
    """Normalised outcome of an :class:`EngineeringAgent` run.

    Mirrors the M1 ``EngineeringResult`` contract: one task in, one result out,
    regardless of how many capabilities actually executed. For a single-capability
    run ``result`` carries the capability payload; for a multi-capability run
    ``supervisor`` carries the full Supervisor + Handoff aggregate.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: SoftwareTask
    kinds: list[TaskKind] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    orchestration: str = "single"  # "single" | "multi-agent"
    result: CapabilityRunResult | None = None
    supervisor: SupervisorResult | None = None
    findings: list[Finding] = Field(default_factory=list)
    verification_passed: bool = True
    degradations: list[str] = Field(default_factory=list)
    error: str | None = None
    dry_run: bool = False

    @classmethod
    def from_supervisor(
        cls, task: SoftwareTask, sup: SupervisorResult
    ) -> EngineeringResult:
        findings: list[Finding] = []
        if sup.result is not None:
            findings.extend(_collect_findings(sup.result))
        for sub in sup.sub_results or []:
            findings.extend(_collect_findings(sub))
        kinds = [
            TaskKind(k) if isinstance(k, str) else k
            for k in (sup.capability_kinds or [])
        ]
        return cls(
            task=task,
            kinds=kinds,
            capabilities=list(sup.capability_kinds or []),
            orchestration=sup.orchestration,
            result=sup.result,
            supervisor=sup if sup.orchestration == "multi-agent" else None,
            findings=findings,
            verification_passed=sup.verification_passed,
            degradations=list(sup.degradations or []),
            error=None,
        )


class EngineeringAgent:
    """One-object API for the whole five-capability engineering agent.

    Construction parameters mirror :class:`DependencyUpgradeAgent` (and the CLI
    flags) so the same configuration drives code, CLI, MCP and the demo:

    - ``mode`` — ``fake`` (offline, deterministic), ``replay`` or ``live``;
    - ``model`` / ``api_key`` / ``base_url`` — live-model configuration;
    - ``budget_tokens`` — cap on total token consumption;
    - ``disable_thinking`` — disable reasoning-model thinking (qwen3.x-plus);
    - ``allow_writes`` / ``allowed_capabilities`` — capability permission gate.

    Unlike :class:`DependencyUpgradeAgent` (which is bound to dependency upgrade),
    this agent routes *any* of the five capabilities from a natural-language goal.
    """

    def __init__(
        self,
        *,
        mode: str = "fake",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        budget_tokens: int | None = None,
        disable_thinking: bool = False,
        allow_writes: bool = False,
        allowed_capabilities: Iterable[str] | None = None,
        replay_dir: str | Path | None = None,
        recording_dir: str | Path | None = None,
    ) -> None:
        settings = Settings()
        model_mode = ModelMode(mode)
        resolved_key = ""
        if api_key:
            resolved_key = api_key
        elif settings.model_api_key is not None:
            resolved_key = settings.model_api_key.get_secret_value()
        self._config = ModelConfig(
            mode=model_mode,
            model=model or settings.model_name,
            api_key=resolved_key,
            base_url=base_url or settings.model_base_url or "",
            max_total_tokens=budget_tokens or settings.model_max_total_tokens,
            disable_thinking=disable_thinking or settings.model_disable_thinking,
        )
        self._mode = model_mode
        self._budget_tokens = budget_tokens or settings.model_max_total_tokens
        self._allow_writes = allow_writes
        self._allowed_capabilities = (
            tuple(allowed_capabilities) if allowed_capabilities is not None else None
        )
        self._replay_dir = str(replay_dir) if replay_dir else None
        self._recording_dir = str(recording_dir) if recording_dir else None

    # -- public API -------------------------------------------------------- #

    def run(
        self,
        goal: str,
        *,
        repo: str | Path | None = None,
        dependency: str | None = None,
        target_version: str | None = None,
        source_version: str | None = None,
        unified_diff: str | None = None,
        issue_text: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        out_dir: str | Path | None = None,
        dry_run: bool = False,
        locale: str = "zh-CN",
    ) -> EngineeringResult:
        """Run a natural-language ``goal`` through the unified execution layer.

        Steps:
            1. Route ``goal`` to a :class:`SoftwareTask` (offline rules; live refines).
            2. Override the routed context with any explicit, trusted kwargs.
            3. Delegate to ``run_supervisor`` (single capability -> ``dispatch_by_task``;
               multiple -> isolated handoffs + unified verification gate).
            4. Return a normalised :class:`EngineeringResult`.

        Args:
            goal: the user's request in natural language (optionally with a
                github.com URL, which is validated before any model call).
            repo: repository root (github URL or local path) for review/security/issue.
            dependency / target_version / source_version: upgrade context.
            unified_diff: a PR/branch diff for pr_review / security_review.
            issue_text: a bug report for issue_repair.
            from_version / to_version: for breaking_change comparison.
            out_dir: advisory; the unified execution layer returns in-memory results.
                The demo/CLI consumes this to persist artifacts.
            dry_run: only route + decompose, do not execute any capability.
            locale: language hint for the model prompt.
        """
        # out_dir / locale are advisory for the caller (demo/CLI persistence and
        # prompt language); the in-memory execution layer does not require them.
        _ = (out_dir, locale)

        gateway = self._make_gateway()
        router = Router(gateway=gateway if gateway.mode != ModelMode.FAKE else None)
        task = router.route_task(goal)

        ctx_updates: dict[str, Any] = {}
        if repo is not None:
            ctx_updates["repo"] = str(repo)
        if dependency:
            ctx_updates["dependency"] = dependency
        if target_version:
            ctx_updates["target_version"] = target_version
        if source_version:
            ctx_updates["source_version"] = source_version
        if unified_diff is not None:
            ctx_updates["unified_diff"] = unified_diff
        if issue_text is not None:
            ctx_updates["issue_text"] = issue_text
        if from_version is not None:
            ctx_updates["from_version"] = from_version
        if to_version is not None:
            ctx_updates["to_version"] = to_version
        if ctx_updates:
            new_ctx = task.context.model_copy(update=ctx_updates)
            task = task.model_copy(update={"context": new_ctx})

        agent_ctx = AgentContext(
            mode=self._mode.value,
            budget_tokens=self._budget_tokens,
            allow_writes=self._allow_writes,
            allowed_capabilities=self._allowed_capabilities,
        )

        if dry_run:
            kinds = decompose_task(task, agent_ctx)
            return EngineeringResult(
                task=task,
                kinds=kinds,
                capabilities=[k.value for k in kinds],
                orchestration="multi-agent" if len(kinds) > 1 else "single",
                degradations=[] if kinds else ["no-capability-matched"],
                error=None if kinds else "未能从任务描述中分诊出任何能力",
                dry_run=True,
            )

        try:
            sup = run_supervisor(task, agent_ctx, mode=self._mode.value)
        except Exception as exc:  # surface, never crash the caller
            logger.exception("engineering agent run failed")
            return EngineeringResult(
                task=task,
                kinds=[task.kind],
                capabilities=[task.kind.value],
                error=f"{type(exc).__name__}: {exc}",
            )
        return EngineeringResult.from_supervisor(task, sup)

    # -- internals -------------------------------------------------------- #

    def _make_gateway(self) -> ModelGateway:
        return ModelGateway(
            self._config,
            replay_dir=self._replay_dir,
            recording_dir=self._recording_dir,
        )
