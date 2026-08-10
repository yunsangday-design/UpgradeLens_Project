"""ROADMAP Step 3 — ReAct tool-calling loop over the registered tools.

In live mode the LLM decides which *collection* tools to call and in what order;
the harness then runs the analysis (``run_assessment`` + ``verify_report``) as a
hard verify gate (A5). In fake mode (or whenever a live run cannot proceed) the
loop falls back to the deterministic ``run_pipeline`` (A4) so a result is always
produced.

The loop deliberately exposes only the evidence-collection tools to the model
(``clone_repo``, ``scan_dependency``, ``scan_code``, ``resolve_skill``,
``retrieve_docs``). The final analysis step is owned by the harness because it
requires the evidence bundle and the verify gate -- the model signals "I have
enough" by returning ``done=true`` (or by hitting the step/budget limit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeEvidenceSummary
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.graph.state import AssessmentSpec
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.models.impact import build_bundle
from upgradelens.pipeline import AssessmentOutcome, EvidenceCollection, analyse, run_pipeline
from upgradelens.tools.registry import (
    ToolContext,
    default_registry,
    resolve_skill_package,
)

_COLLECTION_TOOLS = ("clone_repo", "scan_dependency", "scan_code", "resolve_skill", "retrieve_docs")


class ToolCallDecision(BaseModel):
    """One ReAct turn: the next tool to call (or ``null`` to finish)."""

    thought: str = Field(default="")
    tool: str | None = Field(default=None, description="Next tool to call, or null to finish.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    done: bool = Field(default=False)


@dataclass
class _Accumulator:
    repo_path: Path | None = None
    slug: str | None = None
    ref: str = "main"
    code_report: CodeEvidenceReport | None = None
    skill_id: str | None = None
    doc_runs: list[RetrievalRun] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


def run_agent(
    request: Any,
    gateway: ModelGateway,
    ctx: ToolContext,
    *,
    registry: Any = None,
    max_steps: int = 8,
) -> AssessmentOutcome:
    """Run an assessment either via the ReAct loop (live) or deterministically (fake)."""
    registry = registry or default_registry()
    if gateway.mode == ModelMode.FAKE:
        return run_pipeline(request, gateway, ctx, registry=registry)
    return _run_react(request, gateway, ctx, registry=registry, max_steps=max_steps)


def _collection_tool_specs(registry: Any, request: Any) -> list[dict[str, Any]]:
    allowed = set(_COLLECTION_TOOLS)
    if not (request.db and request.source_id):
        allowed.discard("retrieve_docs")
    return [spec for spec in registry.specs() if spec["name"] in allowed]


def _run_react(
    request: Any,
    gateway: ModelGateway,
    ctx: ToolContext,
    *,
    registry: Any,
    max_steps: int,
) -> AssessmentOutcome:
    acc = _Accumulator()
    specs = _collection_tool_specs(registry, request)

    for step in range(1, max_steps + 1):
        budget = getattr(gateway, "budget", None)
        if budget is not None and budget.remaining_tokens <= 0:
            acc.degradations.append("budget exhausted; stopped early")
            break
        decision = _decide(gateway, specs, request, acc, step)
        if decision.done or not decision.tool:
            break
        ok, observation = _execute(registry, decision, acc, request, ctx)
        acc.history.append(
            {"step": step, "tool": decision.tool, "ok": ok, "observation": observation}
        )

    # Graceful fallback: if the model never produced a usable checkout + scan,
    # the deterministic pipeline guarantees a correct result (A4).
    if acc.repo_path is None or acc.code_report is None:
        acc.degradations.append(
            "react loop did not complete evidence collection; used deterministic pipeline"
        )
        return run_pipeline(request, gateway, ctx, registry=registry)

    collection = _build_collection(acc, request)
    return analyse(collection, gateway, ctx, registry=registry)


def _decide(
    gateway: ModelGateway, specs: list[dict[str, Any]], request: Any, acc: _Accumulator, step: int
) -> ToolCallDecision:
    collected: list[str] = []
    if acc.repo_path:
        collected.append(f"cloned repo -> {acc.repo_path}")
    if acc.code_report:
        collected.append(
            f"scanned code ({acc.code_report.dependency_name}, "
            f"{acc.code_report.scanned_files} files)"
        )
    if acc.skill_id:
        collected.append(f"resolved skill -> {acc.skill_id}")
    if acc.doc_runs:
        chunks = sum(len(run.top_doc_evidence) for run in acc.doc_runs)
        collected.append(f"retrieved docs ({chunks} chunks)")
    collected_block = "\n".join(f"- {line}" for line in collected) or "(nothing collected yet)"

    history_block = (
        "\n".join(
            f"step {h['step']} -> {h['tool']}: {'ok' if h['ok'] else 'error'}: {h['observation']}"
            for h in acc.history
        )
        or "(no steps yet)"
    )

    tool_block = "\n".join(f"- {spec['name']}: {spec['description']}" for spec in specs)

    prompt = (
        "You are driving a dependency-upgrade assessment by calling one tool per turn.\n"
        f"Task: upgrade `{request.dependency}` to `{request.target_version}` "
        f"in repo `{request.repo}`.\n\n"
        "Available tools:\n"
        f"{tool_block}\n\n"
        "Already collected:\n"
        f"{collected_block}\n\n"
        "History:\n"
        f"{history_block}\n\n"
        "Decide the next tool call. clone_repo must run before scan_code/scan_dependency. "
        "When you have cloned, scanned code, resolved the skill, and decided on docs "
        "(or chosen to skip them), set done=true with tool=null. "
        "Respond as JSON: tool (string or null), arguments (object), thought (string), done (bool)."
    )
    decision, _ = gateway.complete_structured(
        prompt=prompt, schema=ToolCallDecision, name=f"agent_loop__{step}"
    )
    return decision


def _build_args(tool: str, args: dict[str, Any], acc: _Accumulator, request: Any) -> dict[str, Any]:
    if tool == "clone_repo":
        if not request.repo:
            raise ValueError("clone_repo needs a repo URL")
        return {"url": request.repo, "ref": request.ref}
    if tool in ("scan_code", "scan_dependency"):
        if acc.repo_path is None:
            raise ValueError("clone_repo must run before scan_code/scan_dependency")
        base = {"repo": str(acc.repo_path), "dependency": request.dependency}
        if tool == "scan_dependency":
            base["target_version"] = request.target_version or ""
        return base
    if tool == "resolve_skill":
        return {"dependency": request.dependency, "target_version": request.target_version}
    if tool == "retrieve_docs":
        if not (request.db and request.source_id):
            raise ValueError("no doc store configured; skip retrieve_docs")
        query = args.get("query") or f"{request.dependency} {request.target_version} migration"
        return {
            "db": request.db,
            "source_id": request.source_id,
            "query": query,
            "top_k": int(args.get("top_k", 5)),
        }
    raise ValueError(f"unhandled tool: {tool}")


def _execute(
    registry: Any, decision: ToolCallDecision, acc: _Accumulator, request: Any, ctx: ToolContext
) -> tuple[bool, str]:
    tool = decision.tool
    if tool not in _COLLECTION_TOOLS:
        return False, f"unknown or disallowed tool: {tool}"
    try:
        args = _build_args(tool, decision.arguments, acc, request)
    except ValueError as exc:
        return False, str(exc)
    try:
        result = registry.run(tool, args, ctx)
    except Exception as exc:  # ToolError / validation error -> feed back, let the model retry
        return False, f"{type(exc).__name__}: {exc}"
    return _absorb(tool, result, acc)


def _absorb(tool: str, result: dict[str, Any], acc: _Accumulator) -> tuple[bool, str]:
    if tool == "clone_repo":
        acc.repo_path = Path(result["path"])
        acc.slug = result.get("slug")
        acc.ref = result.get("ref") or "main"
        return True, f"cloned -> {acc.repo_path}"
    if tool == "scan_code":
        acc.code_report = CodeEvidenceReport.model_validate(result)
        return True, (
            f"scanned code ({acc.code_report.dependency_name}, "
            f"{acc.code_report.scanned_files} files)"
        )
    if tool == "resolve_skill":
        acc.skill_id = result.get("skill_id")
        return True, f"resolved skill -> {acc.skill_id}"
    if tool == "retrieve_docs":
        run = RetrievalRun.model_validate(result)
        acc.doc_runs.append(run)
        return True, f"retrieved docs: {len(run.top_doc_evidence)} chunks"
    if tool == "scan_dependency":
        return True, "scanned dependency"
    return True, "ok"


def _build_collection(acc: _Accumulator, request: Any) -> EvidenceCollection:
    code_report = acc.code_report or CodeEvidenceReport(
        dependency_name=request.dependency,
        scanned_files=0,
        summary=CodeEvidenceSummary(scanned_files=0, usage_count=0),
    )
    repo_path = acc.repo_path
    assert repo_path is not None
    bundle = build_bundle(code_report, acc.doc_runs, dependency=request.dependency)
    skill = (
        resolve_skill_package(request.dependency, request.target_version) if acc.skill_id else None
    )
    spec = AssessmentSpec(
        repo=str(repo_path),
        dependency=request.dependency,
        source_version_spec="",
        target_version_spec=request.target_version or "",
    )
    return EvidenceCollection(
        request=request,
        repo_path=repo_path,
        spec=spec,
        code_report=code_report,
        bundle=bundle,
        skill=skill,
    )
