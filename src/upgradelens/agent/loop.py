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

from upgradelens.agent.coverage import compute_coverage, gap_query, summarize
from upgradelens.agent.plan import (
    FAILED,
    PENDING,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    AgentPlan,
    AgentPlanStep,
    PlanStatus,
)
from upgradelens.agent.planner import build_agent_plan
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.domain.dependency import DependencyScanResult, ResolutionStatus
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.llm.query_rewrite import rewrite_query
from upgradelens.pipeline import (
    COVERAGE_INSUFFICIENT,
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
from upgradelens.verify.models import RemediationKind, classify_issue

logger = logging.getLogger(__name__)

_COLLECTION_TOOLS = ("clone_repo", "scan_dependency", "scan_code", "retrieve_for_package")

# ROADMAP Step 4: cap focused supplementary retrievals per run so the loop always
# terminates even when the doc store cannot cover some symbols.
_MAX_SUPPLEMENTARY = 2

# ROADMAP Step 5: cap verification/remediation rounds so the feedback loop always
# terminates even when the verifier keeps finding remediable issues.
_MAX_REPLANS = 3


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
    coverage_insufficient: bool = False


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
        out = {
            "db": str(request.db),
            "package": request.dependency,
            "source_version": acc.source_version_spec,
            "target_version": acc.target_version_spec,
            "user_intent": request.target_version or request.source_version or "",
            "code_symbols": symbols,
            "source_id": request.source_id or None,
            "top_k": 5,
        }
        if isinstance(decision, ToolCallDecision):
            curated = decision.arguments.get("curated_queries")
        else:
            curated = decision.get("curated_queries") if isinstance(decision, dict) else None
        if curated:
            out["curated_queries"] = curated
        return out
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
    if acc.coverage_insufficient:
        degradations.append(COVERAGE_INSUFFICIENT)
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
    if tool == "supplement_retrieval":
        # ROADMAP Step 4: run as a post-collection phase in ``_run_driven`` (works
        # for both fake and live). The plan-driven policy must not try to execute
        # it as a registry tool (it is not one), so it stays pending here.
        return "wait"
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


def _select_policy(gateway: ModelGateway, request: Any, repo_is_url: bool, plan_writer: Any) -> Any:
    if gateway.mode == ModelMode.LIVE:
        return _ReactPolicy(gateway, request)
    return _PlanDrivenPolicy(request, repo_is_url, plan_writer)


# --- ROADMAP Step 4: coverage assessment + supplementary retrieval ------------- #


def _build_supplementary_query(
    gap: Any, *, acc: _Accumulator, request: Any, gateway: ModelGateway
) -> str:
    """Phrase the focused supplementary query for one gap.

    ``fake`` mode uses the deterministic :func:`gap_query` template. ``live`` mode
    asks the LLM to rewrite a focused query (reusing ``gateway.complete_structured``
    + the shared ``rewrite_query`` path); we fall back to the template on any error
    or empty result.
    """
    if gateway is not None and gateway.mode == ModelMode.LIVE:
        try:
            queries = rewrite_query(
                gateway,
                package=request.dependency,
                code_symbols=[gap.symbol],
                user_intent=request.target_version or request.source_version or "",
                source_version=acc.source_version_spec,
                target_version=acc.target_version_spec,
            )
            if queries:
                return queries[0]
        except Exception:  # noqa: BLE001 - fall back to the deterministic template
            pass
    return gap_query(
        gap,
        package=request.dependency,
        source_version=acc.source_version_spec,
        target_version=acc.target_version_spec,
        user_intent=request.target_version or request.source_version or "",
    )


def _run_supplement(
    plan: AgentPlan,
    step: AgentPlanStep,
    acc: _Accumulator,
    request: Any,
    registry: ToolRegistry,
    ctx: ToolContext,
    gateway: ModelGateway,
    plan_writer: Any,
    max_supplementary: int,
) -> str:
    """Run focused supplementary retrievals until coverage is closed or capped.

    Each iteration picks the highest-priority *un-attempted* gap (by usage count,
    then symbol), performs one focused ``retrieve_for_package`` call, re-checks
    coverage, and records the new trace events under the ``supplement_retrieval``
    plan step. When gaps remain after ``max_supplementary`` attempts the run is
    flagged ``acc.coverage_insufficient`` so the final report degrades and the
    plan notes the shortfall (``needs_human``).
    """
    assert acc.code_report is not None
    coverage = compute_coverage(acc.code_report, acc.doc_runs)
    supplementary_count = 0
    attempted: set[str] = set()

    while coverage.gaps and supplementary_count < max_supplementary:
        candidates = [g for g in coverage.gaps if g.symbol not in attempted]
        if not candidates:
            break
        candidates.sort(key=lambda g: (-g.usage_count, g.symbol))
        gap = candidates[0]

        args = {
            "db": str(request.db),
            "package": request.dependency,
            "source_version": acc.source_version_spec,
            "target_version": acc.target_version_spec,
            "user_intent": request.target_version or request.source_version or "",
            "code_symbols": [gap.symbol],
            "source_id": request.source_id or None,
            "top_k": 5,
            "curated_queries": [
                _build_supplementary_query(gap, acc=acc, request=request, gateway=gateway)
            ],
        }

        ctx.active_plan_step_id = step.id
        ctx.active_attempt = step.attempt
        n0 = len(acc.doc_runs)
        e0 = len(ctx.trace.events)
        try:
            result = registry.run("retrieve_for_package", args, ctx)
            _absorb("retrieve_for_package", result, acc, request)
        except Exception as exc:  # noqa: BLE001 - record the failure, keep going
            logger.warning("supplementary retrieval for %s failed: %s", gap.symbol, exc)
        new_runs = acc.doc_runs[n0:]
        for ev in ctx.trace.events[e0:]:
            ev.plan_step_id = step.id
            ev.evidence_ids = [r.run_id for r in new_runs]

        attempted.add(gap.symbol)
        supplementary_count += 1
        coverage = compute_coverage(acc.code_report, acc.doc_runs)
        _sync_plan(plan_writer, plan)

    summary = summarize(coverage, supplementary_count)
    plan.coverage = summary
    if coverage.gaps:
        acc.coverage_insufficient = True
        plan.notes.append(
            f"evidence coverage insufficient: {coverage.uncovered_symbols}/"
            f"{coverage.total_symbols} symbols uncovered after {supplementary_count} "
            f"supplementary retrieval(s)"
        )

    observation = (
        f"coverage {summary.coverage_rate:.0%} over {summary.total_symbols} code symbols; "
        f"{supplementary_count} supplementary retrieval(s); "
        f"{coverage.uncovered_symbols} gap(s) remaining"
    )
    _sync_plan(plan_writer, plan)
    return observation


def _run_supplement_phase(
    plan: AgentPlan,
    acc: _Accumulator,
    request: Any,
    registry: ToolRegistry,
    ctx: ToolContext,
    gateway: ModelGateway,
    plan_writer: Any,
    max_supplementary: int,
) -> None:
    """Post-collection coverage phase, shared by fake and live modes."""
    step = next((s for s in plan.steps if s.tool == "supplement_retrieval"), None)
    if step is None:
        return
    if step.status in (SUCCEEDED, FAILED):
        return
    if request.db is None:
        step.mark_skipped(_skip_reason("retrieve_for_package", request))
        _sync_plan(plan_writer, plan)
        return
    if acc.code_report is None or acc.repo_path is None:
        step.mark_skipped("no code evidence collected; nothing to cover")
        _sync_plan(plan_writer, plan)
        return
    step.mark_running()
    _sync_plan(plan_writer, plan)
    observation = _run_supplement(
        plan, step, acc, request, registry, ctx, gateway, plan_writer, max_supplementary
    )
    step.mark_outcome(True, observation)
    _sync_plan(plan_writer, plan)


def _remediation_queries(acc: _Accumulator, request: Any, issues: list[Any]) -> list[str]:
    """Build focused retrieval queries from the SUPPLEMENT-class verifier issues."""
    dep = request.dependency or (acc.scan_result.dependency_name if acc.scan_result else "")
    queries: list[str] = []
    for issue in issues:
        if classify_issue(issue.code) is not RemediationKind.SUPPLEMENT:
            continue
        parts = [p for p in (dep, issue.evidence_id, issue.detail) if p]
        if parts:
            queries.append(" ".join(parts))
    # de-dup while preserving order
    unique: list[str] = []
    for q in queries:
        if q not in unique:
            unique.append(q)
    return unique[:_MAX_SUPPLEMENTARY]


def _append_remediation_step(
    plan: AgentPlan,
    acc: _Accumulator,
    request: Any,
    registry: ToolRegistry,
    ctx: ToolContext,
    gateway: ModelGateway,
    plan_writer: Any,
    tool: str,
    *,
    curated_queries: list[str] | None = None,
    repo_is_url: bool = False,
) -> AgentPlanStep:
    """Append and execute one remediation step; return the step for inspection."""
    step = AgentPlanStep(
        id=f"r{plan.replan_count}-{tool}",
        tool=tool,
        seq=len(plan.steps) + 1,
        status=PENDING,
        phase="remediate",
        reason=f"auto-remediation for verifier feedback ({tool})",
    )
    plan.steps.append(step)
    if _evaluate_step(tool, acc, request, repo_is_url) == SKIPPED:
        step.mark_skipped(_skip_reason(tool, request))
        _sync_plan(plan_writer, plan)
        return step
    step.mark_running()
    _sync_plan(plan_writer, plan)
    decision = ToolCallDecision(
        tool=tool,
        arguments={"curated_queries": curated_queries} if curated_queries else {},
    )
    ok, observation = _execute(registry, decision, acc, request, ctx)
    step.mark_outcome(ok, observation)
    if ok and ctx.trace.events:
        ctx.trace.events[-1].evidence_ids = _evidence_ids(tool, acc)
    _sync_plan(plan_writer, plan)
    return step


def _run_remediation(
    plan: AgentPlan,
    acc: _Accumulator,
    request: Any,
    registry: ToolRegistry,
    ctx: ToolContext,
    gateway: ModelGateway,
    plan_writer: Any,
    kinds: set[RemediationKind],
    issues: list[Any],
) -> None:
    """Execute the concrete remediation steps for the issue kinds found this round."""
    repo_is_url = is_repo_url(request.repo)
    if RemediationKind.RESCAN in kinds:
        _append_remediation_step(
            plan,
            acc,
            request,
            registry,
            ctx,
            gateway,
            plan_writer,
            "scan_code",
            repo_is_url=repo_is_url,
        )
    if RemediationKind.SUPPLEMENT in kinds:
        queries = _remediation_queries(acc, request, issues)
        _append_remediation_step(
            plan,
            acc,
            request,
            registry,
            ctx,
            gateway,
            plan_writer,
            "retrieve_for_package",
            curated_queries=queries,
            repo_is_url=repo_is_url,
        )
    # REANALYSE has nothing to re-collect; the next analyse() re-runs model analysis.


def _run_verification_loop(
    plan: AgentPlan,
    acc: _Accumulator,
    request: Any,
    registry: ToolRegistry,
    ctx: ToolContext,
    gateway: ModelGateway,
    plan_writer: Any,
    max_replans: int,
    *,
    analyse_fn: Any = analyse,
) -> AssessmentOutcome:
    """ROADMAP Step 5: feed verifier issues back into a bounded re-plan loop.

    After the main collection the run is verified. Any *remediable* issue
    (supplement / rescan / re-analyse) triggers a remediation step and a fresh
    verification round, up to ``max_replans`` times. Non-remediable issues and
    exhausted budgets end the run as ``needs_human`` / ``budget_exhausted`` so the
    loop always terminates. Every report-producing path runs ``verify_report`` at
    least once because ``analyse_fn`` already verifies internally.
    """
    budget = getattr(gateway, "budget", None)
    remediable_kinds = {
        RemediationKind.SUPPLEMENT,
        RemediationKind.RESCAN,
        RemediationKind.REANALYSE,
    }
    last_collection: EvidenceCollection | None = None
    last_outcome: AssessmentOutcome | None = None
    stopped = "settled"  # settled | budget | rounds

    for round_idx in range(max_replans + 1):
        last_collection = _build_collection(acc, request)
        outcome = analyse_fn(last_collection, gateway, ctx, registry=registry)
        last_outcome = outcome
        issues = [issue for risk in outcome.verified.all_risks for issue in risk.issues]
        plan.unresolved_risks = issues
        kinds = {classify_issue(issue.code) for issue in issues}
        if ctx.trace is not None:
            ctx.trace.record(
                tool="verification_round",
                target=f"round-{round_idx}",
                status="ok",
                params={"issue_codes": sorted(i.code.value for i in issues)},
                plan_step_id=ctx.active_plan_step_id,
                attempt=ctx.active_attempt,
            )
        if not (kinds & remediable_kinds):
            stopped = "settled"
            break
        # Need remediation: stop before spending more if the budget is gone or the
        # round cap is reached.
        if budget is not None and budget.remaining_tokens <= 0:
            stopped = "budget"
            break
        if plan.replan_count >= max_replans:
            stopped = "rounds"
            break
        plan.replan_count += 1
        _run_remediation(plan, acc, request, registry, ctx, gateway, plan_writer, kinds, issues)

    if stopped == "budget":
        plan.status = PlanStatus.BUDGET_EXHAUSTED.value
    elif stopped == "rounds":
        plan.status = PlanStatus.NEEDS_HUMAN.value
    else:
        degradations = list(last_collection.degradations) if last_collection else []
        if last_outcome is not None:
            degradations = degradations or list(last_outcome.verified.degradations)
        plan.status = (
            PlanStatus.COMPLETED_WITH_DEGRADATION.value
            if degradations
            else PlanStatus.COMPLETED.value
        )
    _sync_plan(plan_writer, plan)

    if last_outcome is None:
        plan.degrade_to_pipeline = True
        plan.status = PlanStatus.FAILED.value
        _sync_plan(plan_writer, plan)
        return run_pipeline(request, gateway, ctx, registry=registry)
    return last_outcome


def _run_driven(
    request: Any,
    gateway: ModelGateway,
    ctx: ToolContext,
    registry: ToolRegistry,
    plan: AgentPlan,
    plan_writer: Any,
    repo_is_url: bool,
    max_turns: int,
    max_supplementary: int = _MAX_SUPPLEMENTARY,
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

    # ROADMAP Step 4: coverage assessment + autonomous supplementary retrieval.
    # Runs as a post-collection phase so both the fake plan-driven walk and the
    # live ReAct model converge on the same behaviour (the model is never offered
    # ``supplement_retrieval`` as a registry tool).
    _run_supplement_phase(
        plan, acc, request, registry, ctx, gateway, plan_writer, max_supplementary
    )

    if acc.repo_path is None or acc.code_report is None:
        plan.degrade_to_pipeline = True
        plan.status = PlanStatus.FAILED.value
        _sync_plan(plan_writer, plan)
        return run_pipeline(request, gateway, ctx, registry=registry)

    return _run_verification_loop(
        plan, acc, request, registry, ctx, gateway, plan_writer, _MAX_REPLANS
    )


def run_agent(
    request: Any,
    gateway: ModelGateway,
    ctx: ToolContext,
    *,
    registry: ToolRegistry | None = None,
    plan: AgentPlan | None = None,
    plan_writer: Any = None,
    max_turns: int = 24,
    max_supplementary: int = _MAX_SUPPLEMENTARY,
) -> AssessmentOutcome:
    """Run the agent loop for ``request`` driven by a live :class:`AgentPlan`.

    The plan is built on demand when ``plan`` is ``None`` (fake mode or a planning
    failure yields the deterministic linear plan). ``plan_writer`` is called after
    every step update so the plan.json artifact stays coherent even on a crash.

    ``max_supplementary`` caps focused supplementary retrievals (S4); set it to
    ``0`` to disable the coverage phase (used by the S8 ablation harness).
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
    return _run_driven(
        request,
        gateway,
        ctx,
        registry,
        plan,
        plan_writer,
        repo_is_url,
        max_turns,
        max_supplementary=max_supplementary,
    )


# Re-export the decision schema for the gateway's record-and-replay layer.
__all__ = ["run_agent", "build_agent_plan", "ToolCallDecision", "AgentPlan"]
