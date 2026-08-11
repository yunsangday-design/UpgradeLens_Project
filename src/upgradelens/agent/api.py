"""DependencyUpgradeAgent — the single object API for UpgradeLens (S9).

Wraps the routing, planning, collection, verification and optional run-artifact
writing into one callable object so that CLI, MCP, demo scripts and notebooks
all drive the same kernel::

    from upgradelens import DependencyUpgradeAgent

    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run("upgrade pydantic in ./repo to 2.0")
    print(result.verified.conclusion)

The agent falls back to the deterministic pipeline (``run_pipeline``) when the
plan-driven loop cannot produce a report, and never writes to the repository
being analysed — the pipeline is read-only.

The legacy function-based entry points (``run_agent``, ``run_pipeline``) remain
available; this class is the recommended front door for new code.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from upgradelens.agent.loop import run_agent
from upgradelens.agent.plan import AgentPlan
from upgradelens.agent.planner import build_agent_plan
from upgradelens.agent.router import Intent, Router
from upgradelens.agent.run_store import RunStore
from upgradelens.config import Settings
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.pipeline import AssessmentOutcome, AssessmentRequest, run_pipeline
from upgradelens.tools.live_repo import is_repo_url
from upgradelens.tools.registry import ToolContext, ToolRegistry, default_registry
from upgradelens.tools.trace import ToolTrace

logger = logging.getLogger(__name__)

__all__ = ["DependencyUpgradeAgent", "AgentResult"]


@dataclass
class AgentResult:
    """The outcome of a ``DependencyUpgradeAgent.run()`` call.

    Carries everything a caller (CLI, MCP, demo) needs:

    - ``intent`` — the routed :class:`Intent`;
    - ``outcome`` — the verified assessment (``None`` for non-upgrade intents);
    - ``plan`` — the :class:`AgentPlan` the run followed (``None`` for non-upgrade);
    - ``trace`` — the tool-call trace;
    - ``run_dir`` — the directory artifacts were written to (``None`` if no store);
    - ``degradations`` — accumulated degradation notes;
    - ``gateway`` — the :class:`ModelGateway` used (for ledger/cost inspection).
    """

    intent: Intent
    outcome: AssessmentOutcome | None = None
    plan: AgentPlan | None = None
    trace: ToolTrace | None = None
    run_dir: Path | None = None
    degradations: tuple[str, ...] = ()
    gateway: ModelGateway | None = None
    error: str | None = None

    @property
    def verified(self) -> Any:
        """Shortcut to the :class:`VerifiedReport` (``None`` if not assessed)."""
        return self.outcome.verified if self.outcome is not None else None


class DependencyUpgradeAgent:
    """One-object API for dependency upgrade impact assessment.

    Parameters mirror the CLI flags so the same configuration works everywhere:

    - ``mode`` — ``fake`` (offline, deterministic), ``replay`` (recorded live
      responses) or ``live`` (real LLM);
    - ``model`` / ``api_key`` / ``base_url`` — live-model configuration;
    - ``budget_tokens`` — cap on total token consumption;
    - ``replay_dir`` — directory of recorded responses (``replay`` mode);
    - ``recording_dir`` — where to persist live responses (``live`` mode);
    - ``registry`` — custom tool registry (defaults to the built-in set);
    - ``max_turns`` / ``max_supplementary`` — agent-loop bounds.

    Example::

        agent = DependencyUpgradeAgent(mode="fake")
        result = agent.run("upgrade pydantic in ./repo to 2.0")
        if result.verified:
            print(result.verified.conclusion)

    The agent is stateless between calls — create a fresh instance or reuse
    one; each ``run()`` builds its own gateway, plan and context.
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
        replay_dir: str | Path | None = None,
        recording_dir: str | Path | None = None,
        registry: ToolRegistry | None = None,
        max_turns: int = 24,
        max_supplementary: int = 2,
    ) -> None:
        settings = Settings()
        model_mode = ModelMode(mode)
        # Resolve the API key from the explicit arg, then the settings secret.
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
        self._registry = registry or default_registry()
        self._max_turns = max_turns
        self._max_supplementary = max_supplementary
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
        db: str | Path | None = None,
        source_id: str | None = None,
        ref: str | None = None,
        out_dir: str | Path | None = None,
        dry_run: bool = False,
    ) -> AgentResult:
        """Assess a dependency upgrade from a natural-language ``goal``.

        1. Route the text to an :class:`Intent`;
        2. If ``upgrade_task``, build a plan and run the agent loop;
        3. Optionally persist artifacts to ``out_dir``;
        4. Return an :class:`AgentResult`.

        For non-upgrade intents the method returns early with
        ``outcome=None``.

        ``dry_run`` routes and plans but does not execute the assessment.
        """
        gateway = self._make_gateway()
        router = Router(gateway=gateway if gateway.mode != ModelMode.FAKE else None)
        intent = router.route(goal)

        # Explicit kwargs override / fill in what the router could not extract.
        # This lets callers pass a local repo path that the URL-gate does not
        # recognise, while still benefiting from NL extraction of the goal.
        if repo is not None:
            intent = intent.model_copy(update={"repo": str(repo)})
        if dependency is not None:
            intent = intent.model_copy(update={"dependency": dependency})
        if target_version is not None:
            intent = intent.model_copy(update={"target_version": target_version})
        if source_version is not None:
            intent = intent.model_copy(update={"source_version": source_version})

        # If the explicit kwargs filled all gaps, promote to upgrade_task.
        if (
            intent.kind == "need_clarification"
            and intent.repo
            and intent.dependency
            and intent.target_version
        ):
            intent = intent.model_copy(update={"kind": "upgrade_task", "confidence": 0.95})

        run_id = self._derive_run_id(goal, repo, dependency, target_version)
        store: RunStore | None = None
        if out_dir is not None:
            store = RunStore.create(Path(out_dir), run_id)

        intent_dict = intent.model_dump(mode="json")
        if store is not None:
            store.write_intent(intent_dict)

        if intent.kind != "upgrade_task":
            if store is not None:
                store.write_plan(intent=intent_dict)
                store.write_run_md(
                    intent=intent_dict,
                    mode=gateway.mode.value,
                    verified=None,
                    degradations=(),
                )
            return AgentResult(
                intent=intent, gateway=gateway, run_dir=store.run_dir if store else None
            )

        # -- upgrade_task: build plan + execute -- #
        plan_repo = str(repo) if repo is not None else (intent.repo or "")
        plan_dep = dependency or intent.dependency or ""
        plan_tgt = target_version or intent.target_version or ""
        plan_src = source_version or intent.source_version
        repo_is_url = is_repo_url(plan_repo) if plan_repo else True

        plan = build_agent_plan(
            gateway=gateway,
            registry=self._registry,
            repo=plan_repo,
            dependency=plan_dep,
            target_version=plan_tgt,
            source_version=plan_src,
            request_id=run_id,
            repo_is_url=repo_is_url,
        )
        if store is not None:
            store.write_plan(intent=intent_dict, plan=plan)

        if dry_run:
            return AgentResult(
                intent=intent,
                plan=plan,
                gateway=gateway,
                run_dir=store.run_dir if store else None,
            )

        if not plan_repo or not plan_dep:
            return AgentResult(
                intent=intent,
                plan=plan,
                gateway=gateway,
                run_dir=store.run_dir if store else None,
                error="repo and dependency are required for assessment",
            )

        request = AssessmentRequest(
            repo=plan_repo,
            dependency=plan_dep,
            target_version=plan_tgt,
            source_version=plan_src,
            db=Path(db) if db else None,
            source_id=source_id,
            ref=ref,
        )
        try:
            with ToolContext() as ctx:
                outcome = run_agent(
                    request,
                    gateway,
                    ctx,
                    registry=self._registry,
                    plan=plan,
                    plan_writer=(
                        (lambda p: store.write_plan(intent=intent_dict, plan=p))
                        if store is not None
                        else None
                    ),
                    max_turns=self._max_turns,
                    max_supplementary=self._max_supplementary,
                )
                trace = ctx.trace
        except Exception as exc:
            logger.exception("agent run failed")
            if store is not None:
                store.write_run_md(
                    intent=intent_dict,
                    mode=gateway.mode.value,
                    verified=None,
                    degradations=(f"ERROR: {exc}",),
                )
            return AgentResult(
                intent=intent,
                plan=plan,
                gateway=gateway,
                run_dir=store.run_dir if store else None,
                error=str(exc),
            )

        if store is not None:
            store.write_trace(trace)
            store.write_report(outcome.verified)
            store.write_assessment(outcome)
            store.write_run_md(
                intent=intent_dict,
                mode=gateway.mode.value,
                verified=outcome.verified,
                degradations=tuple(outcome.degradations),
            )

        return AgentResult(
            intent=intent,
            outcome=outcome,
            plan=plan,
            trace=trace,
            run_dir=store.run_dir if store else None,
            degradations=tuple(outcome.degradations),
            gateway=gateway,
        )

    def run_pipeline(
        self,
        repo: str | Path,
        dependency: str,
        *,
        target_version: str | None = None,
        source_version: str | None = None,
        db: str | Path | None = None,
        source_id: str | None = None,
        ref: str | None = None,
    ) -> AssessmentOutcome:
        """Run the deterministic pipeline directly (no agent loop).

        Useful as a baseline or when the agent loop is not needed.
        """
        gateway = self._make_gateway()
        request = AssessmentRequest(
            repo=str(repo),
            dependency=dependency,
            target_version=target_version,
            source_version=source_version,
            db=Path(db) if db else None,
            source_id=source_id,
            ref=ref,
        )
        with ToolContext() as ctx:
            return run_pipeline(request, gateway, ctx, registry=self._registry)

    # -- internals -------------------------------------------------------- #

    def _make_gateway(self) -> ModelGateway:
        return ModelGateway(
            self._config,
            replay_dir=self._replay_dir,
            recording_dir=self._recording_dir,
        )

    @staticmethod
    def _derive_run_id(
        text: str,
        repo: str | Path | None,
        dependency: str | None,
        target_version: str | None,
    ) -> str:
        parts = "|".join(
            [
                text,
                str(repo) if repo else "",
                dependency or "",
                target_version or "",
            ]
        )
        return hashlib.sha1(parts.encode()).hexdigest()[:8]
