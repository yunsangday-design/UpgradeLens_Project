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

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from packaging.utils import canonicalize_name
from sqlalchemy.orm import Session

from upgradelens.analyzers import scan_code_evidence
from upgradelens.analyzers import scan_dependency as scan_dependency_fn
from upgradelens.config import Settings
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import ingest_skill, retrieve
from upgradelens.domain import DependencyAnalysisRequest
from upgradelens.domain.skill import SkillPackage
from upgradelens.eval import BASELINES, render_summary_markdown, run_evaluation
from upgradelens.graph import AssessmentSpec, retrieve_skill_evidence, run_assessment
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import build_bundle
from upgradelens.patch import generate_patch_draft
from upgradelens.skills import SkillParseError, SkillRegistry, builtin_registry
from upgradelens.tools.live_repo import clone_live_repo, is_repo_url
from upgradelens.verify import verify_report
from upgradelens.verify.version_match import extract_version

#: Default on-disk cache for fetched documents (stage 7 cache-first strategy).
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "upgradelens"

#: Shipped Core eval fixtures, resolved relative to the installed package.
DEFAULT_CASES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "eval"

mcp = FastMCP("upgradelens")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_skill(dependency: str, target_version_spec: str | None) -> SkillPackage | None:
    """Find the Skill Pack that serves ``dependency`` (mirrors ``cli._resolve_skill``)."""
    registry = builtin_registry()
    concrete = extract_version(target_version_spec or "")
    if concrete:
        try:
            selection = registry.select_skill(dependency, concrete)
        except SkillParseError:
            selection = None
        if selection is not None:
            found = registry.get(selection.skill_id)
            if found is not None:
                return found
    canonical = canonicalize_name(dependency.strip())
    for skill in registry.all():
        if canonical in skill.canonical_package_names:
            return skill
    return None


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
def ingest_docs(db: str, skill: str = "pydantic_v1_to_v2") -> dict[str, Any]:
    """Stage 4: load a built-in documentation snapshot into the SQLite evidence store.

    Args:
        db: SQLite database path to ingest into.
        skill: Skill Pack id whose documentation snapshots should be ingested.
    """
    package = builtin_registry().get(skill)
    if package is None:
        return {"error": f"unknown skill '{skill}'"}
    session = _open_session(Path(db))
    try:
        records = ingest_skill(session, package)
        return {
            "db": str(db),
            "skill_id": skill,
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
    mode: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    budget_tokens: int | None = None,
    allow_quality_patch: bool = False,
    emit_patch: str | None = None,
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
        mode: Model gateway mode (fake | replay | live).
        model: Model name (live mode).
        api_key: API key (live mode, overrides env).
        base_url: OpenAI-compatible base url (live mode).
        budget_tokens: Maximum total tokens for the assessment.
        allow_quality_patch: Also draft patches whose rules require a quality model.
        emit_patch: Write a generated Unified Diff patch draft to this path.
        record_replay: Record/reply directory for live/replay modes.
        ref: Git branch/tag to clone when ``repo`` is a GitHub URL.
    """
    live_handle = None
    if is_repo_url(repo):
        live_handle = clone_live_repo(repo, ref)
    repo_path = live_handle.path if live_handle is not None else Path(repo)

    code_report = scan_code_evidence(repo_path, dependency)
    skill = _resolve_skill(dependency, target_version)

    session = _open_session(Path(db)) if db is not None else None
    degradations: list[str] = []
    try:
        doc_evidences = (
            retrieve_skill_evidence(session, skill, source_id=source_id)
            if (session is not None and skill is not None)
            else []
        )
        if session is None:
            degradations.append(
                "No documentation index was provided (db); "
                "risks cannot reach 'verified' without doc evidence."
            )
        if skill is None:
            degradations.append(
                f"No Skill Pack matched '{dependency}'; "
                "severity rules fall back to generic scoring."
            )
        bundle = build_bundle(code_report, doc_evidences, dependency=dependency)
        target_version_spec = target_version or (skill.target_version_spec if skill else "") or ""
        source_version_spec = getattr(code_report, "version", "") or ""
        spec = AssessmentSpec(
            repo=str(repo_path),
            dependency=dependency,
            target_version_spec=target_version_spec,
            source_version_spec=source_version_spec,
        )
        gateway = _build_gateway(mode, model, api_key, base_url, budget_tokens, record_replay)
        report = run_assessment(spec, bundle, gateway, skill=skill)
        verified = verify_report(
            report,
            repo_root=repo_path,
            bundle=bundle,
            code_report=code_report,
            skill=skill,
            degradations=degradations,
        )
    finally:
        if session is not None:
            session.close()
        if live_handle is not None:
            live_handle.cleanup()

    result = verified.model_dump(mode="json")

    if emit_patch is not None:
        if skill is None or not skill.allow_patch_draft:
            result["patch_warning"] = "skill does not allow patch drafts; nothing written"
        else:
            draft = generate_patch_draft(
                repo_path,
                verified.verified_risks,
                skill,
                bundle,
                quality_model_available=allow_quality_patch,
            )
            patch_text = draft.to_unified_diff()
            if patch_text:
                Path(emit_patch).write_text(patch_text, encoding="utf-8")
                result["patch"] = patch_text
            else:
                result["patch_warning"] = (
                    "no patch draft generated "
                    "(no eligible verified rewrite at the reported locations)"
                )
    return result


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

    skill = _resolve_skill(dependency, target_version)

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
                rec = ingest_live_source(session, source, fetcher, refresh=refresh)
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


def serve() -> None:  # pragma: no cover - process entry point
    """Entry point for the ``upgradelens-mcp`` script (stdio by default)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    serve()
