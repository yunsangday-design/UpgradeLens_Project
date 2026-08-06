"""Command line interface for UpgradeLens (plan sections 8.10 and 1652).

The CLI is deliberately thin: validate arguments, call the analyzer, print the
JSON contract, return an exit code. It contains no parsing rules of its own, so
the CLI and any future API return byte-identical documents.

Subcommands:

- ``scan-dependency`` (stage 1) — how a dependency is declared and how it
  compares to a target version;
- ``scan-code`` (stage 2) — where a dependency is used in Python source, as AST
  code evidence;
- ``list-skills`` (stage 3) — list the built-in Skill Packs;
- ``resolve-skill`` (stage 3) — pick the best Skill Pack for a dependency +
  target version (generic fallback when nothing matches);
- ``ingest-docs`` (stage 4) — load built-in documentation snapshots into the
  SQLite + FTS5 index;
- ``retrieve-docs`` (stage 4) — run keyword RAG over an ingested documentation
  source and return citable evidence.
- ``fetch-docs`` (stage 7) — fetch a dependency's documentation live from the
  web (PyPI + skill-declared sources), cache-first, and ingest it into the
  SQLite evidence store. Every fetch is recorded in a Tool Trace.
- ``assess --repo <url>`` (stage 7) — pass a GitHub URL instead of a local path
  to clone it, analyse it, and clean up the temp checkout.

Exit codes:

- ``0`` — the scan ran to completion (any :class:`ResolutionStatus`, including
  ``not_found``; "the dependency is absent" is a valid answer, not a failure);
- ``1`` — the request itself was rejected, an ``invalid`` result is still
  printed so machine callers always receive the same schema;
- ``2`` — argparse usage error.

Tracebacks are never printed to stdout, and no machine-absolute path ever
enters the JSON document.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ValidationError

from upgradelens.analyzers import scan_code_evidence, scan_dependency
from upgradelens.config import Settings
from upgradelens.db.database import DEFAULT_DB_PATH, engine_for, init_db, session_for
from upgradelens.db.repository import persist_code_report
from upgradelens.docs import ingest_skill, retrieve
from upgradelens.domain import (
    DependencyAnalysisRequest,
    DependencyScanResult,
    IssueCode,
    ParseIssue,
    ResolutionStatus,
)
from upgradelens.domain.skill import SkillPackage
from upgradelens.eval import BASELINES, render_summary_markdown, run_evaluation
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import EvidenceBundle
from upgradelens.patch import PatchDraft, generate_patch_draft
from upgradelens.pipeline import AssessmentOutcome, AssessmentRequest, run_pipeline
from upgradelens.report import render_markdown
from upgradelens.skills import SkillParseError, SkillRegistry, builtin_registry
from upgradelens.tools.cache import DocCache
from upgradelens.tools.errors import ToolError, ToolExecutionError
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.github import GitHubClient
from upgradelens.tools.ingest_live import ingest_live_source, ingest_pypi_changelog
from upgradelens.tools.pypi import PyPIClient
from upgradelens.tools.registry import ToolContext, resolve_skill_package
from upgradelens.tools.trace import ToolTrace
from upgradelens.verify.models import VerifiedReport

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_INVALID_REQUEST = 1
EXIT_USAGE = 2
EXIT_RUNTIME = 3

_SCAN_COMMAND = "scan-dependency"
_SCAN_CODE_COMMAND = "scan-code"
_LIST_SKILLS_COMMAND = "list-skills"
_RESOLVE_SKILL_COMMAND = "resolve-skill"
_INGEST_DOCS_COMMAND = "ingest-docs"
_RETRIEVE_DOCS_COMMAND = "retrieve-docs"
_ASSESS_COMMAND = "assess"
_EVAL_COMMAND = "eval"
_FETCH_DOCS_COMMAND = "fetch-docs"
_MCP_COMMAND = "mcp"
_COMMENT_PR_COMMAND = "comment-pr"

#: Shipped Core fixtures, resolved relative to the installed package so the
#: command works from any working directory.
DEFAULT_CASES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval"

#: Default on-disk cache for fetched documents (stage 7 cache-first strategy).
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "upgradelens"


def _add_assess_pipeline_args(p: argparse.ArgumentParser) -> None:
    """Arguments shared by ``assess`` and ``comment-pr`` (the analysis pipeline)."""
    p.add_argument("--repo", required=True, type=Path, help="Repository root (path or URL).")
    p.add_argument("--dependency", required=True, metavar="NAME", help="Dependency to assess.")
    p.add_argument(
        "--target-version",
        default=None,
        help="Target version spec (defaults to the resolved skill's target).",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional SQLite database with ingested docs for documentation evidence.",
    )
    p.add_argument(
        "--source-id",
        default=None,
        help="Optional documentation source id to scope retrieval (defaults to the skill id).",
    )
    p.add_argument(
        "--mode",
        default=None,
        choices=["fake", "replay", "live"],
        help="Model gateway mode (defaults to UPGRADELENS_MODEL_MODE, then 'fake').",
    )
    p.add_argument("--model", default=None, help="Model name (live mode).")
    p.add_argument("--api-key", default=None, help="API key (live mode, overrides env).")
    p.add_argument("--base-url", default=None, help="OpenAI-compatible base url (live mode).")
    p.add_argument(
        "--record-replay",
        metavar="DIR",
        default=None,
        help="Record every node response to DIR (use with --mode live). Replay mode "
        "later reads from the same DIR to reproduce the run fully offline.",
    )
    p.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="Maximum total tokens for the assessment (model calls rejected beyond it).",
    )
    p.add_argument(
        "--ref",
        default=None,
        help="Git branch/tag to clone when --repo is a GitHub URL (stage 7 live repo).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``upgradelens`` executable."""
    parser = argparse.ArgumentParser(
        prog="upgradelens",
        description=(
            "Static upgrade analysis: dependency manifests (stage 1), "
            "Python AST code evidence (stage 2) and Skill Pack resolution (stage 3)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        _SCAN_COMMAND,
        help="Report how a dependency is declared and how it compares to a target version.",
    )
    scan.add_argument("--repo", required=True, type=Path, help="Repository root to scan.")
    scan.add_argument("--dependency", required=True, help="Dependency name (any casing).")
    scan.add_argument("--target-version", required=True, help="Target PEP 440 version.")
    scan.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "Optional single manifest to scan, relative to --repo. "
            "Defaults to pyproject.toml then requirements.txt."
        ),
    )

    code = subparsers.add_parser(
        _SCAN_CODE_COMMAND,
        help="Report where a dependency is used in Python source (AST code evidence).",
    )
    code.add_argument("--repo", required=True, type=Path, help="Repository root to scan.")
    code.add_argument("--dependency", required=True, help="Dependency name (any casing).")
    code.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional SQLite database to persist the code evidence into (stage 4).",
    )

    ingest_docs = subparsers.add_parser(
        _INGEST_DOCS_COMMAND,
        help="Ingest built-in documentation snapshots into the SQLite index (stage 4).",
    )
    ingest_docs.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH}).",
    )
    ingest_docs.add_argument(
        "--skill",
        default="pydantic_v1_to_v2",
        help="Skill Pack id whose documentation snapshots should be ingested.",
    )

    retrieve_docs = subparsers.add_parser(
        _RETRIEVE_DOCS_COMMAND,
        help="Run keyword RAG over an ingested documentation source (stage 4).",
    )
    retrieve_docs.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH}).",
    )
    retrieve_docs.add_argument("--source", required=True, help="Documentation source id to query.")
    retrieve_docs.add_argument("--query", required=True, help="Keyword query (e.g. 'validator').")
    retrieve_docs.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Maximum number of evidence chunks to return (default: 5).",
    )

    list_skills = subparsers.add_parser(
        _LIST_SKILLS_COMMAND,
        help="List the built-in Skill Packs and their version ranges.",
    )
    list_skills.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Optional directory of Skill Packs to list (defaults to built-in).",
    )

    resolve = subparsers.add_parser(
        _RESOLVE_SKILL_COMMAND,
        help="Resolve the best Skill Pack for a dependency + target version.",
    )
    resolve.add_argument("--dependency", required=True, help="Dependency name (any casing).")
    resolve.add_argument("--target-version", required=True, help="Target PEP 440 version.")
    resolve.add_argument(
        "--source-version",
        default=None,
        help="Optional source PEP 440 version to narrow the match.",
    )

    assess = subparsers.add_parser(
        _ASSESS_COMMAND,
        help="Run the model-backed upgrade impact assessment (stage 5).",
    )
    _add_assess_pipeline_args(assess)
    assess.add_argument(
        "--format",
        default="json",
        choices=["json", "md"],
        help="Output format: machine JSON (default) or a Markdown report.",
    )
    assess.add_argument(
        "--raw",
        action="store_true",
        help="Emit the unverified model report instead of the verified one (debugging).",
    )
    assess.add_argument(
        "--emit-patch",
        type=Path,
        default=None,
        help="Write a generated Unified Diff patch draft to this path (stage 8).",
    )
    assess.add_argument(
        "--allow-quality-patch",
        action="store_true",
        help="Also draft patches whose rules require a quality model (use with care).",
    )

    comment_pr = subparsers.add_parser(
        _COMMENT_PR_COMMAND,
        help="Assess a repo and post the report as a comment on a GitHub PR/issue.",
    )
    _add_assess_pipeline_args(comment_pr)
    comment_pr.add_argument(
        "--slug",
        required=True,
        metavar="OWNER/REPO",
        help="GitHub repo slug where the target PR/issue lives.",
    )
    comment_pr.add_argument(
        "--pr",
        required=True,
        type=int,
        metavar="N",
        help="Pull request or issue number to comment on.",
    )
    comment_pr.add_argument(
        "--token",
        default=None,
        help="GitHub token (defaults to the GITHUB_TOKEN environment variable).",
    )
    comment_pr.add_argument(
        "--max-chars",
        type=int,
        default=None,
        metavar="N",
        help="Truncate the comment to N characters (GitHub caps comments).",
    )
    comment_pr.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and print the comment without posting (offline-safe).",
    )

    fetch_docs = subparsers.add_parser(
        _FETCH_DOCS_COMMAND,
        help="Fetch a dependency's docs live (PyPI + skill sources) and ingest (stage 7).",
    )
    fetch_docs.add_argument(
        "--db",
        type=Path,
        required=True,
        help="SQLite database to ingest the fetched docs into.",
    )
    fetch_docs.add_argument("--dependency", required=True, help="Dependency name (any casing).")
    fetch_docs.add_argument(
        "--target-version",
        default=None,
        help="Target version spec; used to scope the PyPI changelog query.",
    )
    fetch_docs.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore the on-disk cache and re-fetch every source.",
    )
    fetch_docs.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Directory for the fetched-doc cache (default: {DEFAULT_CACHE_DIR}).",
    )
    fetch_docs.add_argument(
        "--format",
        default="md",
        choices=["json", "md"],
        help="Output format: Markdown summary (default) or JSON Tool Trace.",
    )

    mcp_server = subparsers.add_parser(
        _MCP_COMMAND,
        help="Start the UpgradeLens MCP server (requires the 'mcp' extra).",
    )
    mcp_server.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport to serve (default: stdio).",
    )

    evaluate = subparsers.add_parser(
        _EVAL_COMMAND,
        help="Run the offline evaluation over the Core fixtures (stage 6).",
    )
    evaluate.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_DIR,
        help=f"Directory of evaluation cases (default: {DEFAULT_CASES_DIR}).",
    )
    evaluate.add_argument(
        "--baseline",
        action="append",
        default=None,
        choices=sorted(BASELINES),
        help="Baseline to run; repeat to select several (default: all).",
    )
    evaluate.add_argument(
        "--format",
        default="md",
        choices=["json", "md"],
        help="Output format (default: Markdown summary).",
    )
    evaluate.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Exit non-zero if the hybrid baseline pass rate is below this value (0..1).",
    )
    return parser


def _invalid_request_result(
    dependency_name: str, target_version: str, error: ValidationError
) -> DependencyScanResult:
    """Turn a boundary ``ValidationError`` into the standard result contract.

    Only ``loc`` and ``msg`` are used. Pydantic's ``input`` field is dropped on
    purpose because it would echo the raw repository path back into the JSON.
    """
    issues = [
        ParseIssue(
            code=IssueCode.INVALID_REQUEST,
            message=f"{'.'.join(str(part) for part in item['loc']) or 'request'}: {item['msg']}",
        )
        for item in error.errors()
    ]
    return DependencyScanResult(
        requested_name=dependency_name,
        dependency_name=canonicalize_name(dependency_name.strip()),
        status=ResolutionStatus.INVALID,
        target_version=target_version,
        errors=issues,
    )


def _emit(result: object) -> None:
    """Write the result as UTF-8 JSON, independent of console encoding."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if isinstance(result, BaseModel):
        payload = json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False)
    else:
        payload = json.dumps(result, indent=2, ensure_ascii=False)
    sys.stdout.write(payload + "\n")


def _emit_text(text: str) -> None:
    """Write pre-rendered text (Markdown) as UTF-8, bypassing JSON encoding."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text)


def _build_model_config(args: argparse.Namespace, settings: Settings) -> ModelConfig:
    """Resolve the model gateway configuration from CLI flags and settings."""
    mode = (
        ModelMode(args.mode)
        if getattr(args, "mode", None)
        else (ModelMode(settings.model_mode) if settings.model_mode else ModelMode.FAKE)
    )
    api_key = ""
    if getattr(args, "api_key", None):
        api_key = args.api_key
    elif settings.model_api_key is not None:
        api_key = settings.model_api_key.get_secret_value()
    return ModelConfig(
        mode=mode,
        base_url=getattr(args, "base_url", None) or settings.model_base_url,
        model=getattr(args, "model", None) or settings.model_name,
        api_key=api_key,
        max_total_tokens=getattr(args, "budget_tokens", None) or settings.model_max_total_tokens,
    )


def _assess_repo(args: argparse.Namespace, ctx: ToolContext) -> AssessmentOutcome:
    """Run the shared assessment pipeline for ``assess``/``comment-pr``.

    The CLI's only job here is translating argparse's flat namespace into an
    :class:`AssessmentRequest` and mapping failures onto exit codes; the
    sequence itself lives in :mod:`upgradelens.pipeline`, shared with the MCP
    server and the demo.

    ``ctx`` must stay open while the caller uses the result: for a live repo the
    returned ``repo_path`` is a temp checkout that the context owns.
    """
    request = AssessmentRequest(
        repo=str(args.repo),
        dependency=args.dependency,
        target_version=args.target_version,
        db=args.db,
        source_id=args.source_id,
        ref=getattr(args, "ref", None),
    )
    gateway = ModelGateway(
        _build_model_config(args, Settings()),
        recording_dir=getattr(args, "record_replay", None),
    )
    try:
        return run_pipeline(request, gateway, ctx)
    except ToolExecutionError:
        # Our own code broke; that is a runtime fault, not a bad request.
        raise
    except ToolError as exc:
        # The request could not be served: unclonable URL, refused host, ...
        sys.stderr.write(f"upgradelens: cannot analyse repository: {exc}\n")
        raise SystemExit(EXIT_INVALID_REQUEST) from None


def _assess_command(args: argparse.Namespace) -> int:
    """Run the stage 5 closed loop, verify it, and print the report.

    When ``--repo`` is a GitHub URL, the repository is shallow-cloned to a temp
    dir first (stage 7). The checkout lives for as long as the ``ToolContext``
    below, which is deliberately wider than the assessment itself: verification
    and patch drafting both re-read the analysed tree.
    """
    with ToolContext() as ctx:
        try:
            result = _assess_repo(args, ctx)
        except SystemExit as exc:
            return int(exc.code) if exc.code else EXIT_INVALID_REQUEST

        if args.raw:
            _emit(result.report)
            return EXIT_OK

        if args.format == "md":
            _emit_text(render_markdown(result.verified))
        else:
            _emit(result.verified)

        if args.emit_patch is not None:
            _emit_patch_draft(args, result.verified, result.repo_path, result.skill, result.bundle)
    return EXIT_OK


def _comment_pr_command(args: argparse.Namespace) -> int:
    """Assess a repository and post the report as a comment on a GitHub PR/issue.

    The assessment pipeline is identical to ``assess`` (shared via
    :func:`_assess_repo`). When ``--dry-run`` is set the rendered comment is
    printed to stdout and nothing is posted -- useful for local/offline checks.
    """
    with ToolContext() as ctx:
        try:
            result = _assess_repo(args, ctx)
        except SystemExit as exc:
            return int(exc.code) if exc.code else EXIT_INVALID_REQUEST
        body = render_markdown(result.verified, max_chars=args.max_chars)

    token = args.token or os.environ.get("GITHUB_TOKEN")
    trace = ToolTrace()
    fetcher = RestrictedFetcher(trace=trace, cache=None)
    client = GitHubClient(fetcher)

    if args.dry_run:
        sys.stdout.write(body)
        sys.stderr.write("upgradelens: --dry-run set; comment was not posted.\n")
        return EXIT_OK

    try:
        posted = client.comment_pr(args.slug, args.pr, body, token=token)
    except ToolError as exc:
        sys.stderr.write(f"upgradelens: failed to post PR comment: {exc}\n")
        return EXIT_RUNTIME

    url = (posted or {}).get("html_url")
    sys.stderr.write(
        "upgradelens: posted assessment to "
        f"{args.slug}#{args.pr}{(' (' + url + ')') if url else ''}\n"
    )
    return EXIT_OK


def _emit_patch_draft(
    args: argparse.Namespace,
    verified: VerifiedReport,
    repo_path: Path,
    skill: SkillPackage | None,
    bundle: EvidenceBundle,
) -> None:
    """Generate a Unified Diff patch draft and write it (stage 8).

    Never writes to the analysed tree; only to ``--emit-patch``. When the skill
    disallows drafts, or no verified rewrite is eligible, nothing is written.
    """
    if skill is None or not skill.allow_patch_draft:
        sys.stderr.write("upgradelens: skill does not allow patch drafts; nothing written.\n")
        return
    draft: PatchDraft = generate_patch_draft(
        repo_path,
        verified.verified_risks,
        skill,
        bundle,
        quality_model_available=args.allow_quality_patch,
    )
    text = draft.to_unified_diff()
    if not text:
        sys.stderr.write(
            "upgradelens: no patch draft generated "
            "(no eligible verified rewrite at the reported locations).\n"
        )
        return
    args.emit_patch.write_text(text, encoding="utf-8")
    sys.stderr.write(
        f"upgradelens: wrote patch draft to {args.emit_patch} "
        f"({len(draft.files)} file(s), rules: {', '.join(draft.applied_rules) or 'none'})\n"
    )


def _fetch_docs_command(args: argparse.Namespace) -> int:
    """Fetch a dependency's docs live and ingest them (stage 7).

    Uses a cache-first, traced, SSRF-restricted fetcher. Every network call is
    recorded in a Tool Trace so an auditor can see exactly which URLs were hit
    and how many bytes came back (or whether the result was served from cache).
    """
    skill = resolve_skill_package(args.dependency, args.target_version)

    cache = DocCache(Path(args.cache_dir))
    trace = ToolTrace()
    fetcher = RestrictedFetcher(trace=trace, cache=cache)
    pypi = PyPIClient(fetcher)

    engine = engine_for(args.db)
    init_db(engine)
    session = session_for(engine)()

    records: list[object] = []
    try:
        if skill is not None:
            for source in skill.sources:
                if not source.url:
                    continue
                rec = ingest_live_source(session, source, fetcher, refresh=args.refresh)
                if rec is not None:
                    records.append(rec)

        target_spec = args.target_version or (skill.target_version_spec if skill else "") or ""
        try:
            changelog = pypi.changelog(args.dependency, target_spec or None)
        except ToolError as exc:
            sys.stderr.write(f"upgradelens: pypi changelog skipped: {exc}\n")
            changelog = []
        if changelog:
            records.append(
                ingest_pypi_changelog(
                    session, args.dependency, changelog, target_version_spec=target_spec
                )
            )
    finally:
        session.close()

    summary = {
        "dependency": args.dependency,
        "skill_id": skill.skill_id if skill is not None else None,
        "ingested": len(records),
        "network_calls": trace.network_calls(),
        "cache_hits": trace.cache_hits(),
        "network_bytes": trace.network_bytes(),
        "tool_trace": trace.to_dict(),
    }
    if args.format == "md":
        lines = [
            f"# Live doc fetch: {args.dependency}",
            "",
            f"- skill: `{summary['skill_id']}`",
            f"- sources ingested: **{summary['ingested']}**",
            f"- network calls: **{summary['network_calls']}** "
            f"(cache hits: {summary['cache_hits']}, {summary['network_bytes']} bytes)",
            "",
            "## Tool Trace",
            "",
            "| tool | target | status | http | bytes | cache |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for ev in trace.events:
            lines.append(
                f"| {ev.tool} | {ev.target} | {ev.status} | "
                f"{ev.http_status or ''} | {ev.bytes} | {'yes' if ev.cache_hit else 'no'} |"
            )
        _emit_text("\n".join(lines) + "\n")
    else:
        _emit(summary)
    return EXIT_OK


def _eval_command(args: argparse.Namespace) -> int:
    """Run the offline evaluation and print the baseline comparison."""
    try:
        result = run_evaluation(args.cases, baselines=args.baseline)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"upgradelens: {exc}\n")
        return EXIT_INVALID_REQUEST

    if args.format == "md":
        _emit_text(render_summary_markdown(result))
    else:
        _emit(result.to_dict())

    if args.fail_under is not None:
        hybrid = next((s for s in result.summaries if s.baseline == "hybrid"), None)
        if hybrid is not None and hybrid.pass_rate < args.fail_under:
            sys.stderr.write(
                f"upgradelens: hybrid pass rate {hybrid.pass_rate:.0%} "
                f"is below the required {args.fail_under:.0%}\n"
            )
            return EXIT_INVALID_REQUEST
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``upgradelens`` script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == _LIST_SKILLS_COMMAND:
        if args.base_dir is not None:
            registry: SkillRegistry = SkillRegistry.from_directory(args.base_dir)
        else:
            registry = builtin_registry()
        _emit(registry.catalog())
        return EXIT_OK

    if args.command == _RESOLVE_SKILL_COMMAND:
        try:
            selection = builtin_registry().select_skill(
                args.dependency, args.target_version, args.source_version
            )
        except SkillParseError as exc:
            errors = [ParseIssue(code=IssueCode.INVALID_REQUEST, message=str(exc))]
            _emit(
                DependencyScanResult(
                    requested_name=args.dependency,
                    dependency_name=canonicalize_name(args.dependency.strip()),
                    status=ResolutionStatus.INVALID,
                    target_version=args.target_version,
                    errors=errors,
                )
            )
            sys.stderr.write("upgradelens: invalid request\n")
            return EXIT_INVALID_REQUEST
        _emit(selection)
        return EXIT_OK

    if args.command == _SCAN_CODE_COMMAND:
        report = scan_code_evidence(args.repo, args.dependency)
        if args.db is not None:
            engine = engine_for(args.db)
            init_db(engine)
            session = session_for(engine)()
            try:
                persisted = persist_code_report(session, report)
                sys.stderr.write(f"upgradelens: persisted {persisted} code usages to {args.db}\n")
            finally:
                session.close()
        _emit(report)
        return EXIT_OK

    if args.command == _INGEST_DOCS_COMMAND:
        skill = builtin_registry().get(args.skill)
        if skill is None:
            sys.stderr.write(f"upgradelens: unknown skill '{args.skill}'\n")
            return EXIT_INVALID_REQUEST
        engine = engine_for(args.db)
        init_db(engine)
        session = session_for(engine)()
        try:
            records = ingest_skill(session, skill)
            _emit([rec.model_dump(mode="json") for rec in records])
        finally:
            session.close()
        return EXIT_OK

    if args.command == _RETRIEVE_DOCS_COMMAND:
        engine = engine_for(args.db)
        init_db(engine)
        session = session_for(engine)()
        try:
            run = retrieve(session, args.source, args.query, top_k=args.top_k)
            _emit(run)
        finally:
            session.close()
        return EXIT_OK

    if args.command == _ASSESS_COMMAND:
        return _assess_command(args)

    if args.command == _COMMENT_PR_COMMAND:
        return _comment_pr_command(args)

    if args.command == _FETCH_DOCS_COMMAND:
        return _fetch_docs_command(args)

    if args.command == _MCP_COMMAND:
        from upgradelens.mcp.server import mcp as _mcp_server

        _mcp_server.run(transport=args.transport)
        return EXIT_OK

    if args.command == _EVAL_COMMAND:
        return _eval_command(args)

    try:
        request = DependencyAnalysisRequest(
            repository_root=args.repo,
            dependency_name=args.dependency,
            target_version=args.target_version,
            manifest_path=args.manifest,
        )
    except ValidationError as exc:
        result = _invalid_request_result(args.dependency, args.target_version, exc)
        _emit(result)
        sys.stderr.write(f"upgradelens: invalid request ({len(exc.errors())} problem(s))\n")
        return EXIT_INVALID_REQUEST

    _emit(scan_dependency(request))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
