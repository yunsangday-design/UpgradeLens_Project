"""UpgradeLens MCP server.

Mirrors the ``upgradelens`` CLI subcommands as MCP tools so any MCP-capable
client can drive the analyzer. Every tool returns the same JSON contract the
CLI prints, so the CLI and the server are byte-for-byte equivalent.

Run it:

    # stdio (default, what most local MCP clients use)
    uv run --extra mcp upgradelens-mcp
    uv run --extra mcp upgradelens mcp

    # HTTP/SSE for remote clients
    uv run --extra mcp upgradelens mcp --transport sse

A typical ``claude_desktop_config.json`` entry:

    {
      "mcpServers": {
        "upgradelens": {
          "command": "upgradelens-mcp"
        }
      }
    }

The model gateway defaults to ``fake`` (fully offline, no API key). To use a
real LLM either set ``UPGRADELENS_MODEL_MODE=live`` (and friends) in the
environment, pass ``mode``/``model``/``api_key``/``base_url`` to ``assess``,
or point ``assess`` at a recorded ``replay_dir`` with ``mode="replay"``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from packaging.utils import canonicalize_name
from sqlalchemy.orm import Session

from upgradelens.agent.engineering_agent import EngineeringAgent
from upgradelens.agent.supervisor import AgentContext
from upgradelens.agent.supervisor import run_supervisor as supervisor_run
from upgradelens.analyzers import scan_code_evidence
from upgradelens.analyzers import scan_dependency as scan_dependency_fn
from upgradelens.capabilities import CapabilityRegistry, TransformationPack
from upgradelens.capabilities.transformations import resolve_pack_for_dependency
from upgradelens.capabilities.workbench import (
    list_capabilities as list_unified_capabilities_fn,
)
from upgradelens.capabilities.workbench import run_capability as run_capability_fn
from upgradelens.config import Settings
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import DocSourceManifestError, ingest_corpus, ingest_skill, retrieve
from upgradelens.domain import DependencyAnalysisRequest
from upgradelens.eval import BASELINES, render_summary_markdown, run_evaluation
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.patch import generate_patch_draft
from upgradelens.pipeline import AssessmentOutcome, AssessmentRequest, run_pipeline
from upgradelens.plan import PlanMode, build_upgrade_plan, export_plan
from upgradelens.skills import SkillRegistry, builtin_registry
from upgradelens.tools.registry import ToolContext, resolve_skill_package

#: Default on-disk cache for fetched documents (stage 7 cache-first strategy).
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "upgradelens"

#: Shipped Core eval fixtures, resolved relative to the installed package.
DEFAULT_CASES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "eval"

mcp = FastMCP("upgradelens")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_gateway(
    mode: str | None,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    budget_tokens: int | None,
    recording_dir: str | None,
) -> ModelGateway:  # noqa: D401 - internal helper
    """Resolve the model gateway config from explicit args merged with settings."""
    settings = Settings()
    resolved_mode = (
        ModelMode(mode)
        if mode
        else (ModelMode(settings.model_mode) if settings.model_mode else ModelMode.FAKE)
    )
    resolved_key = api_key or (
        settings.model_api_key.get_secret_value() if settings.model_api_key else ""
    )
    config = ModelConfig(
        mode=resolved_mode,
        base_url=base_url or settings.model_base_url,
        model=model or settings.model_name,
        api_key=resolved_key,
        max_total_tokens=budget_tokens or settings.model_max_total_tokens,
        disable_thinking=settings.model_disable_thinking,
    )
    return ModelGateway(config, recording_dir=recording_dir)


def _open_session(db: Path) -> Session:
    """Open a SQLAlchemy session for a SQLite evidence store path."""
    engine = engine_for(db)
    init_db(engine)
    return session_for(engine)()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def scan_dependency(
    repo: str,
    dependency: str,
    target_version: str,
    manifest: str | None = None,
) -> dict[str, Any]:
    """Stage 1: report how a dependency is declared and how it compares to a target version.

    Args:
        repo: Repository root (local path or GitHub URL).
        dependency: Dependency name (any casing).
        target_version: Target PEP 440 version.
        manifest: Optional single manifest to scan, relative to ``repo``.
    """
    request = DependencyAnalysisRequest(
        repository_root=Path(repo),
        dependency_name=dependency,
        target_version=target_version,
        manifest_path=Path(manifest) if manifest else None,
    )
    return scan_dependency_fn(request).model_dump(mode="json")


@mcp.tool()
def scan_code(repo: str, dependency: str, db: str | None = None) -> dict[str, Any]:
    """Stage 2: report where a dependency is used in Python source (AST code evidence).

    Args:
        repo: Repository root (local path or GitHub URL).
        dependency: Dependency name (any casing).
        db: Optional SQLite database to persist the code evidence into.
    """
    report = scan_code_evidence(Path(repo), dependency)
    if db is not None:
        session = _open_session(Path(db))
        try:
            from upgradelens.db.repository import persist_code_report

            persist_code_report(session, report)
        finally:
            session.close()
    return report.model_dump(mode="json")


@mcp.tool()
def list_skills(base_dir: str | None = None) -> dict[str, Any]:
    """Stage 3: list the built-in Skill Packs and their version ranges.

    Args:
        base_dir: Optional directory of Skill Packs to list (defaults to built-in).
    """
    registry: SkillRegistry = (
        SkillRegistry.from_directory(Path(base_dir)) if base_dir else builtin_registry()
    )
    return registry.catalog().model_dump(mode="json")


@mcp.tool()
def resolve_skill(
    dependency: str,
    target_version: str,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Stage 3: pick the best Skill Pack for a dependency + target version.

    Args:
        dependency: Dependency name (any casing).
        target_version: Target PEP 440 version.
        source_version: Optional source PEP 440 version to narrow the match.
    """
    selection = builtin_registry().select_skill(dependency, target_version, source_version)
    return selection.model_dump(mode="json")


@mcp.tool()
def list_capabilities() -> dict[str, Any]:
    """B5: list the optional Capability Packs (transformations) derived from the corpus.

    This is the skill-independent surface: each built-in skill becomes a
    transformation capability. Prefer this over ``list_skills`` for capability
    discovery.
    """
    registry = CapabilityRegistry.from_skills(builtin_registry().all())
    return {"capabilities": registry.catalog()}


@mcp.tool()
def resolve_capability(
    dependency: str,
    target_version: str,
    source_version: str | None = None,
) -> dict[str, Any]:
    """B5: pick the transformation capability for a dependency + target version.

    Args:
        dependency: Dependency name (any casing).
        target_version: Target PEP 440 version.
        source_version: Optional source PEP 440 version to narrow the match.
    """
    selection = builtin_registry().select_skill(dependency, target_version, source_version)
    if selection is None:
        return {"dependency": dependency, "capability_id": None}
    resolved = builtin_registry().get(selection.skill_id)
    if resolved is None:
        return {"dependency": dependency, "capability_id": None}
    pack = TransformationPack.from_skill(resolved)
    return {
        "dependency": dependency,
        "capability_id": pack.id,
        "allow_patch_draft": pack.allow_patch_draft(),
        "patch_rules": [r.id for r in pack.patch_rules()],
    }


@mcp.tool()
def ingest_docs(db: str, manifest: str = "", skill: str = "") -> dict[str, Any]:
    """Stage 4: load documentation snapshots into the shared corpus.

    Args:
        db: SQLite database path to ingest into.
        manifest: Source manifest file, or a corpus directory scanned for
            ``manifest.yaml``. Preferred: adding a dependency to the corpus
            needs no Skill Pack.
        skill: DEPRECATED. Skill Pack id whose own snapshots should be
            ingested; use ``manifest`` instead.
    """
    if bool(manifest) == bool(skill):
        return {"error": "provide exactly one of 'manifest' (preferred) or 'skill' (deprecated)"}

    package = None
    if skill:
        package = builtin_registry().get(skill)
        if package is None:
            return {"error": f"unknown skill '{skill}'"}

    session = _open_session(Path(db))
    try:
        if package is not None:
            records = ingest_skill(session, package)
        else:
            try:
                records = ingest_corpus(session, Path(manifest))
            except DocSourceManifestError as exc:
                return {"error": str(exc)}
        return {
            "db": str(db),
            "manifest": manifest,
            "skill_id": skill,
            "deprecated_skill_ingestion": bool(skill),
            "ingested": [rec.model_dump(mode="json") for rec in records],
        }
    finally:
        session.close()


@mcp.tool()
def retrieve_docs(db: str, source: str, query: str, top_k: int = 5) -> dict[str, Any]:
    """Stage 4: run keyword RAG over an ingested documentation source and return citable evidence.

    Args:
        db: SQLite database with ingested docs.
        source: Documentation source id to query.
        query: Keyword query (e.g. 'validator').
        top_k: Maximum number of evidence chunks to return.
    """
    session = _open_session(Path(db))
    try:
        run = retrieve(session, source, query, top_k=top_k)
        return run.model_dump(mode="json")
    finally:
        session.close()


@mcp.tool()
def assess(
    repo: str,
    dependency: str,
    target_version: str | None = None,
    db: str | None = None,
    source_id: str | None = None,
    source_version: str | None = None,
    mode: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    budget_tokens: int | None = None,
    allow_quality_patch: bool = False,
    emit_patch: str | None = None,
    plan_only: bool = False,
    plan_path: str | None = None,
    plan_mode: str = "patch_draft",
    record_replay: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Stages 5-8: run the model-backed upgrade impact assessment and verification.

    Defaults to the ``fake`` gateway (offline). Pass ``mode="live"`` with
    ``api_key``/``base_url``/``model``, or ``mode="replay"`` with
    ``record_replay="<dir>"``, to drive a real or recorded model.

    Args:
        repo: Repository root (local path or GitHub URL).
        dependency: Dependency name (any casing).
        target_version: Target version spec (defaults to the resolved skill's target).
        db: Optional SQLite database with ingested docs for documentation evidence.
        source_id: Optional documentation source id to scope retrieval.
        source_version: Optional from-version the repo is being upgraded FROM
            (defaults to manifest inference via scan_dependency).
        mode: Model gateway mode (fake | replay | live).
        model: Model name (live mode).
        api_key: API key (live mode, overrides env).
        base_url: OpenAI-compatible base url (live mode).
        budget_tokens: Maximum total tokens for the assessment.
        allow_quality_patch: Also draft patches whose rules require a quality model.
        emit_patch: Write a generated Unified Diff patch draft to this path.
        plan_only: Also build a stable UpgradePlan (S7) and attach it to the result.
        plan_path: Write the UpgradePlan JSON to this path (implies ``plan_only``).
        plan_mode: Plan execution mode (phase 1: patch_draft | sandbox_apply).
        record_replay: Record/reply directory for live/replay modes.
        ref: Git branch/tag to clone when ``repo`` is a GitHub URL.
    """
    request = AssessmentRequest(
        repo=repo,
        dependency=dependency,
        target_version=target_version,
        source_version=source_version,
        db=Path(db) if db is not None else None,
        source_id=source_id,
        ref=ref,
    )
    gateway = _build_gateway(mode, model, api_key, base_url, budget_tokens, record_replay)

    # The context owns any temp checkout: patch drafting below still reads it.
    with ToolContext() as ctx:
        outcome = run_pipeline(request, gateway, ctx)
        result: dict[str, Any] = outcome.verified.model_dump(mode="json")
        if emit_patch is not None:
            result.update(_draft_patch(outcome, Path(emit_patch), allow_quality_patch))
        if plan_only or plan_path is not None:
            plan = build_upgrade_plan(
                outcome, repo_root=outcome.repo_path, mode=PlanMode(plan_mode)
            )
            result["upgrade_plan"] = plan.model_dump(mode="json")
            if plan_path is not None:
                export_plan(plan, Path(plan_path))
    return result


def _draft_patch(
    outcome: AssessmentOutcome, destination: Path, allow_quality_patch: bool
) -> dict[str, Any]:
    """Write a Unified Diff draft for ``outcome``, or explain why there is none.

    LS-1: the mechanical-rewrite capability is resolved from the migrated
    TransformationPack YAML by the assessed dependency -- the deprecated
    SkillPackage (``outcome.skill``) is no longer involved.
    """
    capability = resolve_pack_for_dependency(outcome.report.target_dependency)
    if capability is None or not capability.allow_patch_draft():
        return {"patch_warning": "capability pack does not permit patch drafts; nothing written"}

    draft = generate_patch_draft(
        outcome.repo_path,
        outcome.verified.verified_risks,
        capability,
        outcome.bundle,
        quality_model_available=allow_quality_patch,
    )
    patch_text = draft.to_unified_diff()
    if not patch_text:
        return {
            "patch_warning": (
                "no patch draft generated (no eligible verified rewrite at the reported locations)"
            )
        }
    destination.write_text(patch_text, encoding="utf-8")
    return {"patch": patch_text}


@mcp.tool()
def fetch_docs(
    db: str,
    dependency: str,
    target_version: str | None = None,
    refresh: bool = False,
    cache_dir: str | None = None,
) -> dict[str, Any]:
    """Stage 7: fetch a dependency's docs live (PyPI + skill sources) and ingest them.

    Args:
        db: SQLite database to ingest the fetched docs into.
        dependency: Dependency name (any casing).
        target_version: Target version spec; used to scope the PyPI changelog query.
        refresh: Ignore the on-disk cache and re-fetch every source.
        cache_dir: Directory for the fetched-doc cache.
    """
    from upgradelens.tools.cache import DocCache
    from upgradelens.tools.errors import ToolError
    from upgradelens.tools.fetcher import RestrictedFetcher
    from upgradelens.tools.ingest_live import ingest_live_source, ingest_pypi_changelog
    from upgradelens.tools.pypi import PyPIClient
    from upgradelens.tools.trace import ToolTrace

    skill = resolve_skill_package(dependency, target_version)

    cache = DocCache(Path(cache_dir)) if cache_dir else DocCache(DEFAULT_CACHE_DIR)
    trace = ToolTrace()
    fetcher = RestrictedFetcher(trace=trace, cache=cache)
    pypi = PyPIClient(fetcher)

    session = _open_session(Path(db))
    records: list[object] = []
    try:
        if skill is not None:
            for source in skill.sources:
                if not source.url:
                    continue
                rec = ingest_live_source(
                    session,
                    source,
                    fetcher,
                    refresh=refresh,
                    package_name=canonicalize_name(dependency),
                    source_version_spec=skill.source_version_spec or "",
                )
                if rec is not None:
                    records.append(rec)

        target_spec = target_version or (skill.target_version_spec if skill else "") or ""
        try:
            changelog = pypi.changelog(dependency, target_spec or None)
        except ToolError:
            changelog = []
        if changelog:
            records.append(
                ingest_pypi_changelog(
                    session, dependency, changelog, target_version_spec=target_spec
                )
            )
    finally:
        session.close()

    return {
        "dependency": dependency,
        "skill_id": skill.skill_id if skill is not None else None,
        "ingested": len(records),
        "network_calls": trace.network_calls(),
        "cache_hits": trace.cache_hits(),
        "network_bytes": trace.network_bytes(),
    }


@mcp.tool()
def run_eval(
    cases: str | None = None,
    baseline: list[str] | None = None,
    fail_under: float | None = None,
) -> dict[str, Any]:
    """Stage 6: run the offline evaluation over the Core fixtures (no network, no model).

    Args:
        cases: Directory of evaluation cases (defaults to the shipped fixtures).
        baseline: Baselines to run; repeat to select several (default: all).
        fail_under: If set, include whether the hybrid baseline pass rate met it.
    """
    cases_dir = Path(cases) if cases else DEFAULT_CASES_DIR
    if baseline:
        unknown = [name for name in baseline if name not in BASELINES]
        if unknown:
            return {"error": f"unknown baseline(s): {unknown} (known: {sorted(BASELINES)})"}
    result = run_evaluation(cases_dir, baselines=baseline)
    response = result.to_dict()
    response["markdown"] = render_summary_markdown(result)
    if fail_under is not None:
        hybrid = next((s for s in result.summaries if s.baseline == "hybrid"), None)
        response["pass_under"] = (hybrid.pass_rate >= fail_under) if hybrid else None
    return response


@mcp.tool()
def list_unified_capabilities() -> dict[str, Any]:
    """List the five unified capabilities (M4 surface) exposed via MCP, with schemas.

    These are the same capabilities the Workbench runs: dependency_upgrade,
    pr_review, issue_repair, security_review, breaking_change. This tool tells an
    MCP client which ``kind`` values :func:`run_capability` accepts.
    """
    caps = list_unified_capabilities_fn()
    return {
        "capabilities": [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in caps]
    }


@mcp.tool()
def run_capability(
    kind: str,
    repo: str | None = None,
    dependency: str | None = None,
    target_version: str | None = None,
    source_version: str | None = None,
    unified_diff: str | None = None,
    issue_text: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    mode: str = "fake",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    budget_tokens: int | None = None,
) -> dict[str, Any]:
    """Run ONE of the five unified capabilities and return its CapabilityRunResult.

    The five capabilities (the M4 "unified capabilities" surface):
      - dependency_upgrade
      - pr_review
      - issue_repair
      - security_review
      - breaking_change

    Defaults to ``mode="fake"`` (fully offline, no API key). Pass ``mode="live"``
    with ``api_key``/``base_url``/``model`` to drive a real LLM for the four
    model-backed capabilities (pr_review / issue_repair / security_review /
    breaking_change). ``dependency_upgrade`` reads its gateway from the
    environment in live mode.

    Args:
        kind: Which capability to run (see ``list_unified_capabilities``).
        repo: Repository root (local path or GitHub URL) for review/security/issue.
        dependency / target_version / source_version: upgrade context.
        unified_diff: a PR/branch diff for pr_review / security_review.
        issue_text: a bug report for issue_repair.
        from_version / to_version: for breaking_change comparison.
        mode: fake | live | replay.
        model / api_key / base_url / budget_tokens: live gateway overrides.
    """
    try:
        task_kind = TaskKind(kind)
    except ValueError:
        return {
            "error": f"unknown capability kind: {kind!r}",
            "known_kinds": [k.value for k in TaskKind if k != TaskKind.UNKNOWN],
        }
    if task_kind == TaskKind.UNKNOWN:
        return {
            "error": "kind must be one of the five unified capabilities",
            "known_kinds": [k.value for k in TaskKind if k != TaskKind.UNKNOWN],
        }
    ctx = TaskContext(
        repo=repo or "",
        dependency=dependency or "",
        source_version=source_version or "",
        target_version=target_version or "",
        unified_diff=unified_diff or "",
        issue_text=issue_text or "",
        from_version=from_version or "",
        to_version=to_version or "",
    )
    task = SoftwareTask(
        task_id=f"mcp-{task_kind.value}-{uuid.uuid4().hex[:8]}",
        kind=task_kind,
        goal=f"{task_kind.value} via MCP",
        context=ctx,
    )
    gateway = None
    if model or api_key or base_url or budget_tokens:
        gateway = _build_gateway(mode, model, api_key, base_url, budget_tokens, None)
    result = run_capability_fn(task, gateway=gateway, mode=mode)
    return result.model_dump(mode="json")


@mcp.tool()
def run_supervisor(
    text: str,
    repo: str | None = None,
    dependency: str | None = None,
    target_version: str | None = None,
    source_version: str | None = None,
    unified_diff: str | None = None,
    mode: str = "fake",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    budget_tokens: int | None = None,
    allowed_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Run a natural-language request through the controlled Supervisor + Handoff layer.

    A single-capability request short-circuits to the unified dispatcher; a
    multi-capability request (e.g. "review this PR and run a security scan") fans
    out to isolated sub-agents and aggregates through the unified verification
    gate. Defaults to ``mode="fake"`` (offline).

    Args:
        text: The natural-language task.
        repo: Repository root (enables pr_review / security_review classification).
        dependency / target_version / source_version: upgrade context.
        unified_diff: a diff to attach (review/security).
        mode: fake | live | replay.
        model / api_key / base_url / budget_tokens: live gateway overrides.
        allowed_capabilities: restrict which capabilities the Supervisor may hand off to.
    """
    ctx = TaskContext(
        repo=repo or "",
        dependency=dependency or "",
        source_version=source_version or "",
        target_version=target_version or "",
        unified_diff=unified_diff or "",
    )
    task = SoftwareTask(
        task_id=f"mcp-sup-{uuid.uuid4().hex[:8]}",
        kind=TaskKind.UNKNOWN,
        goal=text,
        context=ctx,
    )
    agent_ctx = AgentContext(
        mode=mode,
        budget_tokens=budget_tokens or 200_000,
        allowed_capabilities=tuple(allowed_capabilities) if allowed_capabilities else None,
    )
    sup = supervisor_run(task, agent_ctx, mode=mode)
    return {
        "orchestration": sup.orchestration,
        "capability_kinds": sup.capability_kinds,
        "result": sup.result.model_dump(mode="json") if sup.result else None,
        "sub_results": [r.model_dump(mode="json") for r in sup.sub_results],
        "summary": sup.summary,
        "verification_passed": sup.verification_passed,
        "degradations": sup.degradations,
    }


@mcp.tool()
def run_task(
    goal: str,
    repo: str | None = None,
    dependency: str | None = None,
    target_version: str | None = None,
    source_version: str | None = None,
    unified_diff: str | None = None,
    issue_text: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    mode: str = "fake",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    budget_tokens: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """A4 unified entry: run a natural-language goal through EngineeringAgent.

    Routes the goal (plus any explicit overrides) to one -- or, for
    multi-capability requests, several -- of the five capabilities and returns
    the normalised ``EngineeringResult``: task, kinds, result (single-capability
    payload), supervisor (multi-agent aggregate), findings, verification_passed,
    degradations, error, dry_run. This is the same object the CLI ``run`` command
    prints, so both surfaces stay byte-for-byte equivalent.

    Args:
        goal: the user's request in natural language (a github.com URL in the
            text is validated before any model call).
        repo: repository root (github URL or local path) for review/security/issue.
        dependency / target_version / source_version: upgrade context.
        unified_diff: a PR/branch diff for pr_review / security_review.
        issue_text: a bug report for issue_repair.
        from_version / to_version: for breaking_change comparison.
        mode: fake | live | replay (default fake, fully offline).
        model / api_key / base_url / budget_tokens: live gateway overrides.
        dry_run: only route + decompose capabilities, do not execute.
    """
    agent = EngineeringAgent(
        mode=mode,
        model=model,
        api_key=api_key,
        base_url=base_url,
        budget_tokens=budget_tokens,
    )
    result = agent.run(
        goal,
        repo=repo,
        dependency=dependency,
        target_version=target_version,
        source_version=source_version,
        unified_diff=unified_diff,
        issue_text=issue_text,
        from_version=from_version,
        to_version=to_version,
        dry_run=dry_run,
    )
    return result.model_dump(mode="json")


def serve() -> None:  # pragma: no cover - process entry point
    """Entry point for the ``upgradelens-mcp`` script (stdio by default)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    serve()
