"""ROADMAP Step 3 -- agent ReAct loop driven by a live :class:`AgentPlan`.

The loop is a single state machine shared by all three gateway modes:

* ``live``   -- a ReAct model decides the next tool each turn.
* ``fake``   -- a deterministic plan-driven policy walks the plan steps.
* ``replay`` -- replays recorded model decisions through the same machine.

Every turn the loop picks (or is handed) the next tool, resolves it to a plan
step, marks the step ``running`` *before* the call and ``succeeded``/``failed``
*after* it, records the produced evidence ids, and writes the plan back
atomically. Tools the model calls that are not already in the plan are recorded
as ad-hoc steps so the plan always explains every action. A tool failure is a
plan outcome (not just a chat message); only when the driven loop cannot reach a
local checkout + code scan does it fall back to :func:`run_pipeline` (kept as the
product baseline / eval harness).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from upgradelens.agent.plan import (
    PENDING,
    RUNNING,
    SKIPPED,
    AgentPlan,
    AgentPlanStep,
)
from upgradelens.agent.planner import build_agent_plan
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.domain.dependency import DependencyScanResult, ResolutionStatus
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.pipeline import (
    NO_DOC_INDEX,
    AssessmentOutcome,
    EvidenceCollection,
    analyse,
    build_evidence_collection,
    run_pipeline,
)
from upgradelens.tools.live_repo import is_repo_url
from upgradelens.tools.registry import (
    ToolContext,
    ToolRegistry,
    default_registry,
    resolve_skill_package,
)

logger = logging.getLogger(__name__)

_COLLECTION_TOOLS = ("clone_repo", "scan_dependency", "scan_code", "retrieve_for_package")


class ToolCallDecision(BaseModel):
    """The next action the policy hands the loop each turn."""

    tool: str | None = Field(default=None, description="Tool to call, or None to finish.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    done: bool = False
    thought: str = ""


@dataclass
class _Accumulator:
    """Mutable scratch space the loop fills as tools run."""

    repo_path: Path | None = None
    scan_result: DependencyScanResult | None = None
    code_report: CodeEvidenceReport | None = None
    doc_runs: list[RetrievalRun] = field(default_factory=list)
    source_version_spec: str = ""
    target_version_spec: str = ""
    skill: Any = None


def _collected_specs(acc: _Accumulator, request: Any) -> tuple[str, str]:
    src = acc.source_version_spec or request.source_version or "unknown"
    tgt = acc.target_version_spec or request.target_version or "unknown"
    return src, tgt


def _collection_tool_specs(
    registry: ToolRegistry, request: Any, repo_is_url: bool
) -> list[dict[str, Any]]:
    available = {spec["name"] for spec in registry.specs()}
    specs: list[dict[str, Any]] = []
    if request.db is None:
        available.discard("retrieve_for_package")
    if not repo_is_url:
        available.discard("clone_repo")
    for spec in registry.specs():
        if spec["name"] in available and spec["name"] in _COLLECTION_TOOLS:
            specs.append(spec)
    return specs


def _decide(
    gateway: ModelGateway,
    specs: list[dict[str, Any]],
    request: Any,
    acc: _Accumulator,
    turn: int,
) -> ToolCallDecision:
    spec_lines = "\n".join(f"- {s['name']}: {s['description']}" for s in specs)
    collected = (
        f"repo_path={acc.repo_path}, code_report={'yes' if acc.code_report else 'no'}, "
        f"doc_runs={len(acc.doc_runs)}, scan_result={'yes' if acc.scan_result else 'no'}"
    )
    prompt = (
        "You are a senior dependency-upgrade analyst collecting evidence before a "
        "structured assessment. Choose the NEXT tool to call (or finish).\n\n"
        "Return JSON: {tool: str|null, arguments: object, done: bool, thought: str}\n"
        "- Call each needed collection tool at most once; prefer `scan_code`/`scan_dependency` "
        "for a local repo, `clone_repo` first for a URL. When evidence is enough, set done=true.\n"
        "- Only call `retrieve_for_package` if a doc store was provided (it wasn't if absent).\n\n"
        f"# Run state (turn {turn})\n{collected}\n\n"
        f"# Available tools\n{spec_lines}\n\n"
        f"# Request\nrepo={request.repo}\ndependency={request.dependency}\n"
        f"target_version={request.target_version}\nsource_version={request.source_version}\n"
    )
    decision, _ = gateway.complete_structured(
        prompt=prompt, schema=ToolCallDecision, name=f"agent_loop__{turn}"
    )
    return decision


def _build_args(
    tool: str,
    decision: ToolCallDecision,
    acc: _Accumulator,
    request: Any,
) -> dict[str, Any]:
    if tool == "clone_repo":
        return {"url": request.repo, "ref": getattr(request, "ref", None)}
    if tool == "scan_dependency":
        if acc.repo_path is None:
            raise ValueError("repo_path not resolved before scan_dependency")
        return {
            "repo": str(acc.repo_path),
            "dependency": request.dependency,
            "target_version": request.target_version or "",
            "manifest": None,
        }
    if tool == "scan_code":
        if acc.repo_path is None:
            raise ValueError("repo_path not resolved before scan_code")
        return {"repo": str(acc.repo_path), "dependency": request.dependency}
    if tool == "retrieve_for_package":
        if acc.repo_path is None:
            raise ValueError("repo_path not resolved before retrieve_for_package")
        if request.db is None:
            raise ValueError("retrieve_for_package requires a doc store (request.db)")
        symbols: list[str] = []
        if acc.code_report is not None:
            symbols = [u.symbol for u in acc.code_report.usages]
        return {
            "db": str(request.db),
            "package": request.dependency,
            "source_version": acc.source_version_spec,
            "target_version": acc.target_version_spec,
            "user_intent": request.target_version or request.source_version or "",
            "code_symbols": symbols,
            "source_id": request.source_id or None,
            "top_k": 5,
        }
    raise ValueError(f"unknown tool: {tool}")


def _execute(
    registry: ToolRegistry,
    decision: ToolCallDecision,
    acc: _Accumulator,
    request: Any,
    ctx: ToolContext,
) -> tuple[bool, str]:
    assert decision.tool is not None, "execute is only called for a concrete tool"
    tool_name: str = decision.tool
    try:
        args = _build_args(tool_name, decision, acc, request)
        result = registry.run(tool_name, args, ctx)
        message = _absorb(tool_name, result, acc, request)
        return True, message
    except Exception as exc:  # noqa: BLE001 - surfaced as a plan failure, not a crash
        logger.warning("tool %s failed: %s", tool_name, exc)
        return False, f"{type(exc).__name__}: {exc}"


def _absorb(tool: str, result: Any, acc: _Accumulator, request: Any) -> str:
    if tool == "clone_repo":
        path = Path(str(result["path"]))
        acc.repo_path = path
        return f"cloned to {path}"
    if tool == "scan_dependency":
        scan = (
            result
            if isinstance(result, DependencyScanResult)
            else DependencyScanResult.model_validate(result)
        )
        acc.scan_result = scan
        if scan.status == ResolutionStatus.RESOLVED and scan.current_specifier:
            acc.source_version_spec = scan.current_specifier
        return f"source version: {acc.source_version_spec} (status={scan.status.value})"
    if tool == "scan_code":
        report = (
            result
            if isinstance(result, CodeEvidenceReport)
            else CodeEvidenceReport.model_validate(result)
        )
        acc.code_report = report
        return f"scanned {len(report.usages)} usages across {report.scanned_files} files"
    if tool == "retrieve_for_package":
        runs = [
            r if isinstance(r, RetrievalRun) else RetrievalRun.model_validate(r)
            for r in (result or [])
        ]
        acc.doc_runs.extend(runs)
        return f"retrieved {len(runs)} doc chunks"
    return "ok"


def _build_collection(acc: _Accumulator, request: Any) -> EvidenceCollection:
    code_report = acc.code_report
    if code_report is None:
        raise ValueError("no code evidence collected; agent failed to reach a checkout+scan")
    src, tgt = _collected_specs(acc, request)
    skill = resolve_skill_package(request.dependency, request.target_version)
    degradations: list[str] = []
    if src in ("unknown", "") or tgt in ("unknown", ""):
        degradations.append("unknown/conflict source version")
    if request.db is None:
        degradations.append(NO_DOC_INDEX)
    return build_evidence_collection(
        request=request,
        repo_path=acc.repo_path or Path(request.repo),
        code_report=code_report,
        doc_runs=acc.doc_runs,
        scan_result=acc.scan_result,
        skill=skill,
        degradations=degradations or None,
    )


# --- plan linkage (ROADMAP Step 3) -------------------------------------------- #


def _resolve_step(plan: AgentPlan, tool: str) -> AgentPlanStep | None:
    """Find the pending/running plan step that owns ``tool``."""
    for step in plan.steps:
        if step.tool == tool and step.status in (PENDING, RUNNING):
            return step
    return None


def _add_adhoc_step(plan: AgentPlan, tool: str, thought: str | None) -> AgentPlanStep:
    step = AgentPlanStep(
        id=f"a{len(plan.steps) + 1}",
        tool=tool,
        seq=len(plan.steps) + 1,
        status=PENDING,
        phase="collect",
        reason=thought or "ad-hoc tool called by model",
    )
    plan.steps.append(step)
    return step


def _sync_plan(plan_writer: Any, plan: AgentPlan) -> None:
    if plan_writer is not None:
        plan_writer(plan)


def _evaluate_step(tool: str, acc: _Accumulator, request: Any, repo_is_url: bool) -> str:
    """Return ``'run'``, ``'skip'`` (never runnable) or ``'wait'`` (not yet)."""
    if tool == "clone_repo":
        return SKIPPED if not repo_is_url else "run"
    if tool in ("scan_dependency", "scan_code"):
        return "run" if acc.repo_path is not None else "wait"
    if tool == "retrieve_for_package":
        if request.db is None:
            return SKIPPED
        return "run" if acc.repo_path is not None else "wait"
    return "run"


def _skip_reason(tool: str, request: Any) -> str:
    if tool == "clone_repo":
        return "local repo path provided; no clone needed"
    if tool == "retrieve_for_package":
        return "no doc store configured; skipping doc retrieval"
    return "not applicable to this request"


def _evidence_ids(tool: str, acc: _Accumulator) -> list[str]:
    if tool == "clone_repo":
        return [str(acc.repo_path)] if acc.repo_path else []
    if tool == "scan_code" and acc.code_report is not None:
        return [f"code:{acc.code_report.dependency_name}"]
    if tool == "scan_dependency":
        return [f"depscan:{acc.source_version_spec}"]
    if tool == "retrieve_for_package":
        return [r.run_id for r in acc.doc_runs]
    return []


class _ReactPolicy:
    """Live mode: a ReAct model decides the next tool (replays in replay mode)."""

    def __init__(self, gateway: ModelGateway, request: Any) -> None:
        self.gateway = gateway
        self.request = request

    def decide(
        self, turn: int, acc: _Accumulator, plan: AgentPlan, specs: list[dict[str, Any]]
    ) -> ToolCallDecision:
        return _decide(self.gateway, specs, self.request, acc, turn)


class _PlanDrivenPolicy:
    """Fake/local mode: walk the plan steps deterministically."""

    def __init__(self, request: Any, repo_is_url: bool, plan_writer: Any) -> None:
        self.request = request
        self.repo_is_url = repo_is_url
        self.plan_writer = plan_writer

    def decide(
        self, turn: int, acc: _Accumulator, plan: AgentPlan, specs: list[dict[str, Any]]
    ) -> ToolCallDecision:
        for step in plan.steps:
            if step.status != PENDING:
                continue
            verdict = _evaluate_step(step.tool, acc, self.request, self.repo_is_url)
            if verdict == "run":
                return ToolCallDecision(
                    tool=step.tool,
                    arguments={},
                    done=False,
                    thought=f"plan step {step.id}: {step.reason}",
                )
            if verdict == SKIPPED:
                step.mark_skipped(_skip_reason(step.tool, self.request))
                _sync_plan(self.plan_writer, plan)
                continue
            # "wait": preconditions not yet met by prior steps; try the rest.
        return ToolCallDecision(
            tool=None, arguments={}, done=True, thought="all plan steps resolved"
        )


def _select_policy(
    gateway: ModelGateway, request: Any, repo_is_url: bool, plan_writer: Any
) -> Any:
    if gateway.mode == ModelMode.LIVE:
        return _ReactPolicy(gateway, request)
    return _PlanDrivenPolicy(request, repo_is_url, plan_writer)


def _run_driven(
    request: Any,
    gateway: ModelGateway,
    ctx: ToolContext,
    registry: ToolRegistry,
    plan: AgentPlan,
    plan_writer: Any,
    repo_is_url: bool,
    max_turns: int,
) -> AssessmentOutcome:
    acc = _Accumulator()
    ctx.gateway = gateway
    ctx.embedding = getattr(gateway, "embedding", None)
    if not repo_is_url:
        acc.repo_path = Path(request.repo)

    policy = _select_policy(gateway, request, repo_is_url, plan_writer)
    specs = _collection_tool_specs(registry, request, repo_is_url)

    for turn in range(1, max_turns + 1):
        budget = getattr(gateway, "budget", None)
        if budget is not None and budget.remaining_tokens <= 0:
            plan.notes.append("budget exhausted; stopped early")
            _sync_plan(plan_writer, plan)
            break

        decision = policy.decide(turn, acc, plan, specs)
        if decision.done or not decision.tool:
            break

        step = _resolve_step(plan, decision.tool)
        if step is None:
            step = _add_adhoc_step(plan, decision.tool, decision.thought)
        step.mark_running()

        ctx.active_plan_step_id = step.id
        ctx.active_attempt = step.attempt

        ok, observation = _execute(registry, decision, acc, request, ctx)
        step.mark_outcome(ok, observation)
        if ok and ctx.trace.events:
            ctx.trace.events[-1].evidence_ids = _evidence_ids(decision.tool, acc)
        _sync_plan(plan_writer, plan)

        if not ok:
            plan.notes.append(f"step {step.id} ({decision.tool}) failed: {observation}")

    if acc.repo_path is None or acc.code_report is None:
        plan.degrade_to_pipeline = True
        _sync_plan(plan_writer, plan)
        return run_pipeline(request, gateway, ctx, registry=registry)

    collection = _build_collection(acc, request)
    return analyse(collection, gateway, ctx, registry=registry)


def run_agent(
    request: Any,
    gateway: ModelGateway,
    ctx: ToolContext,
    *,
    registry: ToolRegistry | None = None,
    plan: AgentPlan | None = None,
    plan_writer: Any = None,
    max_turns: int = 24,
) -> AssessmentOutcome:
    """Run the agent loop for ``request`` driven by a live :class:`AgentPlan`.

    The plan is built on demand when ``plan`` is ``None`` (fake mode or a planning
    failure yields the deterministic linear plan). ``plan_writer`` is called after
    every step update so the plan.json artifact stays coherent even on a crash.
    """
    registry = registry or default_registry()
    repo_is_url = is_repo_url(request.repo)
    if plan is None:
        plan = build_agent_plan(
            gateway=gateway,
            registry=registry,
            repo=request.repo,
            dependency=request.dependency,
            target_version=request.target_version,
            source_version=request.source_version,
            repo_is_url=repo_is_url,
        )
    return _run_driven(request, gateway, ctx, registry, plan, plan_writer, repo_is_url, max_turns)


# Re-export the decision schema for the gateway's record-and-replay layer.
__all__ = ["run_agent", "build_agent_plan", "ToolCallDecision", "AgentPlan"]
