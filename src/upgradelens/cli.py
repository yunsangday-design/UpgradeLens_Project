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
- ``list-capabilities`` (stage 5 / B5) — list the optional Capability Packs
  (transformations) derived from the corpus; the skill-independent surface;
- ``resolve-capability`` (stage 5 / B5) — pick the transformation capability for
  a dependency + target version.
- ``gate`` (stage 6 / 6.1) — CI gate: read a verified report (``assess --format
  json`` artifact) and exit non-zero when a VERIFIED risk is at/above the
  blocking severity.
- ``ingest-docs`` (stage 4) — load built-in documentation snapshots into the
  SQLite + FTS5 index;
- ``retrieve-docs`` (stage 4) — run keyword RAG over an ingested documentation
  source and return citable evidence.
- ``retrieval-baseline`` (step 4, B0) — record the FTS5-only curated retrieval
  baseline (recall@k / MRR / top-k hit) as a reproducible regression guard before
  the shared RAG path replaces the curated queries.
- ``fetch-docs`` (stage 7) — fetch a dependency's documentation live from the
  web (PyPI + skill-declared sources), cache-first, and ingest it into the
  SQLite evidence store. Every fetch is recorded in a Tool Trace.
- ``assess --repo <url>`` (stage 7) — pass a GitHub URL instead of a local path
  to clone it, analyse it, and clean up the temp checkout.
- ``agent "<text>"`` (Step 2) — natural-language entry: route the request with
  the Step-1 router, run the shared assessment, and write a self-contained run
  directory (intent/plan/trace/report/RUN.md) under ``--out`` (default
  ``runs/<run_id>/``).

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
import hashlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ValidationError

from upgradelens.agent.loop import run_agent
from upgradelens.agent.planner import build_agent_plan
from upgradelens.analyzers import scan_code_evidence, scan_dependency
from upgradelens.capabilities import CapabilityRegistry, TransformationPack
from upgradelens.config import NetworkMode, Settings
from upgradelens.db.database import DEFAULT_DB_PATH, engine_for, init_db, session_for
from upgradelens.db.repository import persist_code_report
from upgradelens.docs import DocSourceManifestError, ingest_corpus, ingest_skill, retrieve
from upgradelens.domain import (
    DependencyAnalysisRequest,
    DependencyScanResult,
    IssueCode,
    ParseIssue,
    ResolutionStatus,
)
from upgradelens.domain.skill import SkillPackage
from upgradelens.eval import (
    BASELINES,
    SYSTEMS,
    compare_runs,
    load_cases,
    render_summary_markdown,
    run_ablation,
    run_comparison,
    run_comparison_replay,
    run_evaluation,
)
from upgradelens.eval.retrieval_baseline import (
    load_retrieval_cases,
    render_retrieval_baseline_markdown,
    run_baseline,
)
from upgradelens.gate import gate_report
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.llm.health import check_model
from upgradelens.models.impact import EvidenceBundle
from upgradelens.patch import PatchDraft, generate_patch_draft
from upgradelens.pipeline import (
    AssessmentOutcome,
    AssessmentRequest,
    analyse,
    collect_evidence,
    run_pipeline,
)
from upgradelens.plan import PlanMode, build_upgrade_plan, export_plan
from upgradelens.report import render_markdown
from upgradelens.skills import SkillParseError, SkillRegistry, builtin_registry
from upgradelens.tools.cache import DocCache
from upgradelens.tools.errors import ToolError, ToolExecutionError
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.github import GitHubClient
from upgradelens.tools.ingest_live import ingest_live_source, ingest_pypi_changelog
from upgradelens.tools.pypi import PyPIClient
from upgradelens.tools.registry import ToolContext, default_registry, resolve_skill_package
from upgradelens.tools.trace import ToolTrace
from upgradelens.verify.models import VerifiedReport

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_INVALID_REQUEST = 1
EXIT_USAGE = 2
EXIT_RUNTIME = 3
EXIT_GATE_BLOCKED = 4

_SCAN_COMMAND = "scan-dependency"
_SCAN_CODE_COMMAND = "scan-code"
_LIST_SKILLS_COMMAND = "list-skills"
_RESOLVE_SKILL_COMMAND = "resolve-skill"
_LIST_CAPABILITIES_COMMAND = "list-capabilities"
_RESOLVE_CAPABILITY_COMMAND = "resolve-capability"
_GATE_COMMAND = "gate"
_INGEST_DOCS_COMMAND = "ingest-docs"
_RETRIEVE_DOCS_COMMAND = "retrieve-docs"
_ASSESS_COMMAND = "assess"
_EVAL_COMMAND = "eval"
_EVAL_COMPARE_COMMAND = "eval-compare"
_EVAL_ABLATE_COMMAND = "eval-ablate"
_EVAL_REPLAY_COMMAND = "eval-replay"
_FETCH_DOCS_COMMAND = "fetch-docs"
_MCP_COMMAND = "mcp"
_COMMENT_PR_COMMAND = "comment-pr"
_AGENT_COMMAND = "agent"
_SEED_REPLAY_COMMAND = "seed-replay"
_RETRIEVAL_BASELINE_COMMAND = "retrieval-baseline"
_LLM_CHECK_COMMAND = "llm-check"
_RAG_WORKER_COMMAND = "rag-worker"

#: Shipped Core fixtures, resolved relative to the installed package so the
#: command works from any working directory.
DEFAULT_CASES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval"

#: Shipped retrieval evaluation cases (ROADMAP Step 4, B0), resolved like above.
DEFAULT_RETRIEVAL_CASES_DIR = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "retrieval"
)

#: Default on-disk cache for fetched documents (stage 7 cache-first strategy).
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "upgradelens"


def _add_assess_pipeline_args(p: argparse.ArgumentParser, *, require: bool = True) -> None:
    """Arguments shared by ``assess`` and ``comment-pr`` (the analysis pipeline).

    ``require=False`` is used by the ``agent`` command where ``--repo`` and
    ``--dependency`` are optional overrides on top of the routed intent.
    """
    p.add_argument("--repo", required=require, type=Path, help="Repository root (path or URL).")
    p.add_argument("--dependency", required=require, metavar="NAME", help="Dependency to assess.")
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
        "--source-version",
        default=None,
        help="Optional from-version the repo is being upgraded FROM. When omitted, "
        "UpgradeLens infers it from the manifest (scan_dependency).",
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
        "--replay-dir",
        metavar="DIR",
        default=None,
        help="Replay recorded node responses from DIR (use with --mode replay). "
        "Populate it via 'seed-replay' (canned, offline) or "
        "'assess --mode live --record-replay DIR' (real capture).",
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
        help="Ingest documentation snapshots into the shared corpus (stage 4).",
    )
    ingest_docs.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH}).",
    )
    ingest_docs.add_argument(
        "--manifest",
        type=Path,
        help=(
            "Source manifest file, or a corpus directory scanned for manifest.yaml. "
            "This is the supported way to add a dependency to the shared corpus."
        ),
    )
    ingest_docs.add_argument(
        "--skill",
        help="DEPRECATED: ingest a Skill Pack's own snapshots. Use --manifest instead.",
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

    # B5: the capability catalog is the new, skill-independent surface. The
    # list-skills / resolve-skill commands above remain as compatibility shims.
    list_caps = subparsers.add_parser(
        _LIST_CAPABILITIES_COMMAND,
        help="(B5) List the optional Capability Packs (transformations) built from the corpus.",
    )
    list_caps.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Optional dir of Skill Packs to derive capabilities from (defaults to built-in).",
    )

    resolve_cap = subparsers.add_parser(
        _RESOLVE_CAPABILITY_COMMAND,
        help="(B5) Resolve the transformation capability for a dependency + target version.",
    )
    resolve_cap.add_argument("--dependency", required=True, help="Dependency name (any casing).")
    resolve_cap.add_argument("--target-version", required=True, help="Target PEP 440 version.")
    resolve_cap.add_argument(
        "--source-version",
        default=None,
        help="Optional source PEP 440 version to narrow the match.",
    )

    # 6.1: CI gate consumes the assess artifact and blocks on verified high risk.
    gate = subparsers.add_parser(
        _GATE_COMMAND,
        help="(6.1) Gate a verified report; exit non-zero on a VERIFIED blocking risk.",
    )
    gate.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Verified report JSON (output of `assess --format json`, or `assess --raw`).",
    )
    gate.add_argument(
        "--block-on",
        default="high,critical",
        help="Comma-separated severities that block (default: high,critical).",
    )
    gate.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
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
        "--plan-only",
        type=Path,
        default=None,
        help="Write a stable UpgradePlan JSON (S7) to this path and exit.",
    )
    assess.add_argument(
        "--plan-mode",
        choices=[m.value for m in PlanMode],
        default=PlanMode.PATCH_DRAFT.value,
        help="Plan execution mode for --plan-only (phase 1: patch_draft | sandbox_apply).",
    )
    assess.add_argument(
        "--allow-quality-patch",
        action="store_true",
        help="Also draft patches whose rules require a quality model (use with care).",
    )

    agent = subparsers.add_parser(
        _AGENT_COMMAND,
        help="Natural-language entry: route the request, run the assessment, write run artifacts.",
    )
    agent.add_argument(
        "text",
        help="Natural-language request, e.g. 'upgrade pydantic in owner/repo to 2.0'.",
    )
    _add_assess_pipeline_args(agent, require=False)
    agent.add_argument(
        "--out",
        type=Path,
        default=Path("runs"),
        help="Base directory for run artifacts; the run lands in <out>/<run_id>/.",
    )

    seed_replay = subparsers.add_parser(
        _SEED_REPLAY_COMMAND,
        help="Record demo canned (evidence-anchored) model responses for offline --mode replay.",
    )
    seed_replay.add_argument(
        "--repo", required=True, type=Path, help="Repository root path or URL."
    )
    seed_replay.add_argument(
        "--dependency", required=True, metavar="NAME", help="Dependency to assess."
    )
    seed_replay.add_argument("--target-version", default=None, help="Target version spec.")
    seed_replay.add_argument(
        "--db", type=Path, default=None, help="Optional SQLite database with ingested docs."
    )
    seed_replay.add_argument("--source-id", default=None, help="Optional documentation source id.")
    seed_replay.add_argument(
        "--source-version",
        default=None,
        help="Optional from-version the repo is being upgraded FROM.",
    )
    seed_replay.add_argument("--model", default=None, help="Model name (metadata only).")
    seed_replay.add_argument(
        "--out",
        type=Path,
        default=Path("replay"),
        help="Directory to write replay JSON files (consumed by --mode replay).",
    )

    llm_check = subparsers.add_parser(
        _LLM_CHECK_COMMAND,
        help="Probe the configured model endpoint with one tiny structured call.",
    )
    llm_check.add_argument(
        "--mode",
        default=None,
        choices=["fake", "replay", "live"],
        help="Model gateway mode (defaults to UPGRADELENS_MODEL_MODE, then 'fake'). "
        "Only 'live' talks to a real endpoint.",
    )
    llm_check.add_argument("--model", default=None, help="Model name (live mode).")
    llm_check.add_argument("--api-key", default=None, help="API key (live mode, overrides env).")
    llm_check.add_argument(
        "--base-url", default=None, help="OpenAI-compatible base url (live mode)."
    )
    llm_check.add_argument(
        "--replay-dir",
        metavar="DIR",
        default=None,
        help="Directory of recorded responses (use with --mode replay).",
    )
    llm_check.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Request timeout in seconds for the probe call (live mode).",
    )
    agent.add_argument(
        "--format",
        default="json",
        choices=["json", "md"],
        help="Stdout output format: machine JSON (default) or a Markdown report.",
    )
    agent.add_argument(
        "--dry-run",
        action="store_true",
        help="Route the request and write intent/plan only; do not run the pipeline.",
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

    # S17: drain the background corpus-backfill queue (online-discovered sources
    # re-ingested locally so future retrievals hit without the network).
    rag_worker = subparsers.add_parser(
        _RAG_WORKER_COMMAND,
        help="Drain pending S17 corpus backfill jobs into the shared corpus.",
    )
    rag_worker.add_argument(
        "--db", default=str(DEFAULT_DB_PATH), help="SQLite database path"
    )
    rag_worker.add_argument(
        "--once", action="store_true", help="process pending jobs once and exit (default)"
    )
    rag_worker.add_argument(
        "--loop", action="store_true", help="keep processing until the queue is empty"
    )
    rag_worker.add_argument("--limit", type=int, default=10, help="max jobs per pass")
    rag_worker.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between passes in --loop mode",
    )
    rag_worker.add_argument(
        "--network",
        choices=[m.value for m in NetworkMode],
        default=NetworkMode.ONLINE_FALLBACK.value,
        help="fetch policy for backfill; never online-fetches in offline mode",
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
    evaluate.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="Path to a previous `eval --format json` output; prints an A/B delta "
        "table attributing metric changes to the prompt version difference.",
    )
    evaluate.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the JSON result (including any comparison) to this path for "
        "later use with --compare.",
    )
    evaluate.add_argument(
        "--retrieval-cases",
        type=Path,
        default=None,
        help="Directory of retrieval cases for the strategy table "
        f"(default: {DEFAULT_RETRIEVAL_CASES_DIR}; skipped on error).",
    )

    eval_compare = subparsers.add_parser(
        _EVAL_COMPARE_COMMAND,
        help="S8 architecture comparison: direct LLM vs fixed pipeline vs agent (offline FAKE).",
    )
    eval_compare.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_DIR,
        help=f"Directory of evaluation cases (default: {DEFAULT_CASES_DIR}).",
    )
    eval_compare.add_argument(
        "--systems",
        action="append",
        default=None,
        choices=list(SYSTEMS),
        help="Architecture to compare; repeat to select several (default: all three).",
    )
    eval_compare.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the Markdown comparison report to this path.",
    )
    eval_compare.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write the JSON comparison report (per-case + aggregate) to this path.",
    )

    eval_ablate = subparsers.add_parser(
        _EVAL_ABLATE_COMMAND,
        help="S8 ablation: isolate verifier / supplement / agent value (offline FAKE).",
    )
    eval_ablate.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_DIR,
        help=f"Directory of evaluation cases (default: {DEFAULT_CASES_DIR}).",
    )
    eval_ablate.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the Markdown ablation report to this path.",
    )
    eval_ablate.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write the JSON ablation report (per-case + aggregate) to this path.",
    )

    eval_replay = subparsers.add_parser(
        _EVAL_REPLAY_COMMAND,
        help="S8 replay comparison: run against recorded live model responses.",
    )
    eval_replay.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_DIR,
        help=f"Directory of evaluation cases (default: {DEFAULT_CASES_DIR}).",
    )
    eval_replay.add_argument(
        "--replay-dir",
        type=Path,
        required=True,
        help="Root directory containing per-case recorded model responses "
        "({replay_dir}/{case_id}/*.json).",
    )
    eval_replay.add_argument(
        "--systems",
        action="append",
        default=None,
        choices=list(SYSTEMS),
        help="Architecture to compare; repeat to select several (default: all three).",
    )
    eval_replay.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the Markdown comparison report to this path.",
    )
    eval_replay.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write the JSON comparison report (per-case + aggregate) to this path.",
    )

    retrieval_baseline = subparsers.add_parser(
        _RETRIEVAL_BASELINE_COMMAND,
        help="Record the FTS5-only curated retrieval baseline (Step 4, B0).",
    )
    retrieval_baseline.add_argument(
        "--db",
        default=None,
        help="SQLite database to build/use (default: a temporary file, removed after).",
    )
    retrieval_baseline.add_argument(
        "--cases-dir",
        type=Path,
        default=None,
        help=f"Directory of retrieval cases (default: {DEFAULT_RETRIEVAL_CASES_DIR}).",
    )
    retrieval_baseline.add_argument(
        "--out",
        default=None,
        help="Write the JSON baseline report to this path.",
    )
    retrieval_baseline.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Per-query top-k used by the curated retrieval path (default: 5).",
    )
    retrieval_baseline.add_argument(
        "--format",
        default="md",
        choices=["json", "md"],
        help="Output format (default: Markdown summary).",
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
        disable_thinking=settings.model_disable_thinking,
    )


def _report_model_usage(
    config: ModelConfig, gateway: ModelGateway, outcome: AssessmentOutcome
) -> None:
    """Make live model usage visible on stderr (stdout stays machine-readable).

    Without this, a live run whose calls all failed is indistinguishable from a
    successful one: the graph degrades to the static report on purpose, so the
    only signal would be a quieter report.
    """
    if config.mode != ModelMode.LIVE:
        return
    ledger = gateway.ledger
    sys.stderr.write(
        f"upgradelens: llm live model={config.model} calls={len(ledger)} "
        f"tokens={sum(r.total_tokens for r in ledger)} "
        f"latency={sum(r.latency_ms for r in ledger)}ms "
        f"cost=${sum(r.cost_usd for r in ledger):.4f}\n"
    )
    if getattr(outcome.report, "static", False):
        sys.stderr.write(
            "upgradelens: WARNING the live model was unavailable; the report fell back "
            "to static analysis (static=true). Diagnose with 'upgradelens llm-check "
            "--mode live'.\n"
        )


def _llm_check_command(args: argparse.Namespace) -> int:
    """Probe the configured endpoint and print the outcome as JSON."""
    config = _build_model_config(args, Settings())
    if args.timeout is not None:
        config = replace(config, request_timeout_seconds=args.timeout)
    if config.mode == ModelMode.REPLAY and not args.replay_dir:
        sys.stderr.write("upgradelens: --mode replay requires --replay-dir (recorded responses).\n")
        return EXIT_INVALID_REQUEST

    health = check_model(config, replay_dir=args.replay_dir)
    _emit(health)
    if not health.ok:
        sys.stderr.write(f"upgradelens: model check failed: {health.error}\n")
        return EXIT_RUNTIME
    if not health.called_real_api:
        sys.stderr.write(f"upgradelens: no real API call was made ({health.note}).\n")
    return EXIT_OK


def _assess_repo(args: argparse.Namespace, ctx: ToolContext) -> AssessmentOutcome:
    """Run the shared assessment pipeline for ``assess``/``comment-pr``.

    The CLI's only job here is translating argparse's flat namespace into an
    :class:`AssessmentRequest` and mapping failures onto exit codes; the
    sequence itself lives in :mod:`upgradelens.pipeline`, shared with the MCP
    server and the demo.

    ``ctx`` must stay open while the caller uses the result: for a live repo the
    returned ``repo_path`` is a temp checkout that the context owns.
    """
    config = _build_model_config(args, Settings())
    if config.mode == ModelMode.REPLAY and not getattr(args, "replay_dir", None):
        sys.stderr.write("upgradelens: --mode replay requires --replay-dir (recorded responses).\n")
        raise SystemExit(EXIT_INVALID_REQUEST)
    request = AssessmentRequest(
        repo=str(args.repo),
        dependency=args.dependency,
        target_version=args.target_version,
        source_version=args.source_version,
        db=args.db,
        source_id=args.source_id,
        ref=getattr(args, "ref", None),
    )
    gateway = ModelGateway(
        config,
        recording_dir=getattr(args, "record_replay", None),
        replay_dir=getattr(args, "replay_dir", None),
    )
    try:
        outcome = run_pipeline(request, gateway, ctx)
        _report_model_usage(config, gateway, outcome)
        return outcome
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

        if args.plan_only is not None:
            plan = build_upgrade_plan(
                result,
                repo_root=result.repo_path,
                mode=PlanMode(args.plan_mode),
            )
            dest = export_plan(plan, args.plan_only)
            sys.stderr.write(
                f"upgradelens: wrote upgrade plan to {dest} "
                f"({len(plan.steps)} step(s), mode={plan.mode.value})\n"
            )
    return EXIT_OK


def _seed_replay_command(args: argparse.Namespace) -> int:
    """Record demo canned (evidence-anchored) model responses so the closed loop
    can run fully offline via ``--mode replay``.

    This is a *placeholder* for a real capture (``assess --mode live
    --record-replay <dir>``); the recorded responses are illustrative fixtures,
    not genuine model reasoning. They are anchored to the real code-usage
    evidence discovered in the target repo so the verifier can still promote
    them to VERIFIED and the loop is exercisable without an API key.
    """
    from upgradelens.llm.fixtures import build_fake_responses

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    request = AssessmentRequest(
        repo=str(args.repo),
        dependency=args.dependency,
        target_version=args.target_version,
        source_version=args.source_version,
        db=args.db,
        source_id=args.source_id,
        ref=None,
    )
    config = ModelConfig(
        mode=ModelMode.FAKE,
        model=args.model or "qwen-plus",
        api_key="",
        base_url="",
    )
    with ToolContext() as ctx:
        collection = collect_evidence(request, ctx)
        responses, _injected = build_fake_responses(
            collection.bundle, args.dependency, collection.skill
        )
        gateway = ModelGateway(
            config,
            fake_responses=responses or None,
            recording_dir=str(out_dir),
        )
        analyse(collection, gateway, ctx)

    written = sorted(p.name for p in out_dir.glob("*.json"))
    if not written:
        sys.stderr.write(
            "upgradelens: seed-replay recorded nothing; the target repo may have "
            "no code-usage evidence for this dependency.\n"
        )
        return EXIT_INVALID_REQUEST
    _emit(
        {
            "replay_dir": str(out_dir),
            "recorded_nodes": written,
            "note": (
                "These are demo canned (evidence-anchored) responses, not real model "
                "outputs. Replace with a real capture: upgradelens assess --mode live "
                "--record-replay <dir> --repo <repo> --dependency <dep> ..."
            ),
        }
    )
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


def _derive_agent_run_id(text: str, repo: object, dependency: object, target: object) -> str:
    """Stable, secret-free run id derived from the request (not the clock).

    Identical natural-language input yields the same id so a second run
    overwrites the first, making ``plan.json`` / ``trace.jsonl`` trivially
    diffable for the Step-2 acceptance check.
    """
    seed = f"{text}|{repo}|{dependency}|{target}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"run-{digest}"


def _agent_command(args: argparse.Namespace) -> int:
    """Natural-language entry point (ROADMAP Step 2).

    Routes the free-text request through the Step-1 router, then -- when the
    intent is an ``upgrade_task`` -- runs the shared analysis pipeline and
    writes a self-contained run directory (intent/plan/trace/report/RUN.md)
    under ``--out``. Non-upgrade intents are still written (auditable) but do
    not trigger the pipeline.
    """
    from upgradelens.agent.router import Router
    from upgradelens.agent.run_store import RunStore

    settings = Settings()
    config = _build_model_config(args, settings)
    if config.mode == ModelMode.REPLAY and not getattr(args, "replay_dir", None):
        sys.stderr.write("upgradelens: --mode replay requires --replay-dir (recorded responses).\n")
        raise SystemExit(EXIT_INVALID_REQUEST)
    gateway = ModelGateway(
        config,
        recording_dir=getattr(args, "record_replay", None),
        replay_dir=getattr(args, "replay_dir", None),
    )
    registry = default_registry()
    run_id = _derive_agent_run_id(args.text, args.repo, args.dependency, args.target_version)
    store = RunStore.create(args.out, run_id)

    intent = Router().route(args.text)
    intent_dict = intent.model_dump(mode="json")
    store.write_intent(intent_dict)
    if intent.kind != "upgrade_task":
        # No assessment to plan for non-upgrade intents.
        store.write_plan(intent=intent_dict)
    else:
        from upgradelens.tools.live_repo import is_repo_url

        plan_repo = str(args.repo) if args.repo is not None else (intent.repo or "")
        plan_dep = args.dependency or (intent.dependency or "")
        plan_tgt = args.target_version or (intent.target_version or "")
        plan_src = intent.source_version
        plan = build_agent_plan(
            gateway=gateway,
            registry=registry,
            repo=plan_repo,
            dependency=plan_dep,
            target_version=plan_tgt,
            source_version=plan_src,
            request_id=run_id,
            repo_is_url=is_repo_url(plan_repo) if plan_repo else True,
        )
        store.write_plan(intent=intent_dict, plan=plan)

    if args.dry_run:
        _emit(intent_dict)
        sys.stderr.write(f"upgradelens: --dry-run; wrote {store.run_dir}\n")
        return EXIT_OK

    if intent.kind != "upgrade_task":
        store.write_run_md(
            intent=intent_dict, mode=config.mode.value, verified=None, degradations=()
        )
        message = intent.clarification or (
            "The request was not recognised as a dependency upgrade task."
        )
        sys.stdout.write(message + "\n")
        if intent.kind == "invalid_url":
            return EXIT_INVALID_REQUEST
        return EXIT_OK

    repo = str(args.repo) if args.repo is not None else intent.repo
    dependency = args.dependency or intent.dependency
    target_version = args.target_version or intent.target_version
    if not repo or not dependency:
        sys.stderr.write("upgradelens: repo and dependency are required for assessment.\n")
        return EXIT_INVALID_REQUEST
    request = AssessmentRequest(
        repo=repo,
        dependency=dependency,
        target_version=target_version,
        source_version=args.source_version or intent.source_version,
        db=args.db,
        source_id=args.source_id,
        ref=getattr(args, "ref", None),
    )
    with ToolContext() as ctx:
        try:
            result = run_agent(
                request,
                gateway,
                ctx,
                registry=registry,
                plan=plan,
                plan_writer=lambda p: store.write_plan(intent=intent_dict, plan=p),
            )
        except ToolError as exc:
            sys.stderr.write(f"upgradelens: cannot analyse repository: {exc}\n")
            return EXIT_INVALID_REQUEST
        store.write_trace(ctx.trace)
        store.write_report(result.verified)
        store.write_run_md(
            intent=intent_dict,
            mode=config.mode.value,
            verified=result.verified,
            degradations=tuple(result.degradations),
        )
        if args.format == "md":
            _emit_text(render_markdown(result.verified))
        else:
            _emit(result.verified)
    sys.stderr.write(f"upgradelens: wrote run artifacts to {store.run_dir}\n")
    return EXIT_OK


def _emit_patch_draft(
    args: argparse.Namespace,
    verified: VerifiedReport,
    repo_path: Path,
    skill: SkillPackage | None,
    bundle: EvidenceBundle,
) -> None:
    """Generate a Unified Diff patch draft and write it (stage 8).

    Never writes to the analysed tree; only to ``--emit-patch``. When no
    capability pack permits drafts, or no verified rewrite is eligible, nothing
    is written. The skill is only read to derive its (optional) transformation
    capability; the patch logic itself no longer depends on the Skill Pack.
    """
    capability = TransformationPack.from_skill(skill) if skill is not None else None
    if capability is None or not capability.allow_patch_draft():
        sys.stderr.write(
            "upgradelens: capability pack does not permit patch drafts; nothing written.\n"
        )
        return
    draft: PatchDraft = generate_patch_draft(
        repo_path,
        verified.verified_risks,
        capability,
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
                rec = ingest_live_source(
                    session,
                    source,
                    fetcher,
                    refresh=args.refresh,
                    package_name=canonicalize_name(args.dependency),
                    source_version_spec=skill.source_version_spec or "",
                )
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


def _retrieval_strategy_section(
    cases_dir: Path | None,
) -> tuple[str, dict[str, Any]] | None:
    """Best-effort retrieval-strategy comparison for the eval summary.

    Surfaced so a single ``eval`` run shows both the model-quality baselines and
    the retrieval strategy (FTS5-only) that feeds them. The standard retrieval
    fixtures are evaluated every time; any problem (missing dir, broken fixture)
    is swallowed so it can never hide a model-quality regression.
    """
    try:
        target = cases_dir or DEFAULT_RETRIEVAL_CASES_DIR
        cases = load_retrieval_cases(target)
    except (ValueError, FileNotFoundError) as _exc:
        sys.stderr.write(f"upgradelens: retrieval section skipped: {_exc!r}\n")
        return None

    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    engine = engine_for(db_path)
    init_db(engine)
    session = session_for(engine)()
    try:
        skills = builtin_registry().all()
        report = run_baseline(session, skills, cases, top_k=5)
    except Exception as _exc:  # noqa: BLE001 - best-effort; never hide a regression
        sys.stderr.write(f"upgradelens: retrieval section skipped: {_exc!r}\n")
        return None
    finally:
        session.close()
        try:
            os.remove(db_path)
        except OSError:
            pass
    return render_retrieval_baseline_markdown(report), report.model_dump()


def _eval_command(args: argparse.Namespace) -> int:
    """Run the offline evaluation and print the baseline + retrieval comparison.

    The command is the fixed point of the prompt-iteration loop: edit a prompt,
    re-run, and pass the previous JSON back with ``--compare`` to read an A/B
    delta. ``--out`` writes the current JSON so the next edit has something to
    diff against.
    """
    try:
        result = run_evaluation(args.cases, baselines=args.baseline)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"upgradelens: {exc}\n")
        return EXIT_INVALID_REQUEST

    # Build the whole payload (comparison + retrieval) up front so the file
    # written by --out and the JSON printed to stdout are identical.
    comparison = None
    if args.compare is not None:
        try:
            previous = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"upgradelens: cannot read --compare file: {exc}\n")
            return EXIT_INVALID_REQUEST
        comparison = compare_runs(result, previous)

    section = _retrieval_strategy_section(args.retrieval_cases)
    retrieval_md = section[0] if section is not None else None
    retrieval_json = section[1] if section is not None else None

    payload = result.to_dict()
    if comparison is not None:
        payload["comparison"] = comparison.to_dict()
    if retrieval_json is not None:
        payload["retrieval"] = retrieval_json

    if args.out is not None:
        Path(args.out).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        sys.stderr.write(f"upgradelens: wrote eval result to {args.out}\n")

    if args.format == "md":
        text = render_summary_markdown(result, comparison=comparison)
        if retrieval_md is not None:
            text = text.rstrip() + "\n\n" + retrieval_md
        _emit_text(text)
    else:
        _emit(payload)

    if args.fail_under is not None:
        hybrid = next((s for s in result.summaries if s.baseline == "hybrid"), None)
        if hybrid is not None and hybrid.pass_rate < args.fail_under:
            sys.stderr.write(
                f"upgradelens: hybrid pass rate {hybrid.pass_rate:.0%} "
                f"is below the required {args.fail_under:.0%}\n"
            )
            return EXIT_INVALID_REQUEST
    return EXIT_OK


def _eval_compare_command(args: argparse.Namespace) -> int:
    """S8 architecture comparison (direct LLM vs fixed pipeline vs agent).

    Fully off-line: model nodes are satisfied by deterministic fakes derived
    from each case's ``model_report.json`` and the run executes in FAKE mode.
    """
    systems = tuple(args.systems) if args.systems else SYSTEMS
    try:
        cases = load_cases(Path(args.cases))
        report = run_comparison(cases, systems=systems)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"upgradelens: {exc}\n")
        return EXIT_INVALID_REQUEST

    markdown = report.to_markdown()
    _emit_text(markdown)

    if args.out is not None:
        Path(args.out).write_text(markdown, encoding="utf-8")
        sys.stderr.write(f"upgradelens: wrote S8 report to {args.out}\n")
    if args.json is not None:
        Path(args.json).write_text(
            json.dumps(report.to_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        sys.stderr.write(f"upgradelens: wrote S8 JSON to {args.json}\n")
    return EXIT_OK


def _eval_ablate_command(args: argparse.Namespace) -> int:
    """S8 ablation: isolate verifier / supplement / agent value (offline FAKE)."""
    try:
        cases = load_cases(Path(args.cases))
        report = run_ablation(cases)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"upgradelens: {exc}\n")
        return EXIT_INVALID_REQUEST

    markdown = report.to_markdown()
    _emit_text(markdown)

    if args.out is not None:
        Path(args.out).write_text(markdown, encoding="utf-8")
        sys.stderr.write(f"upgradelens: wrote S8 ablation report to {args.out}\n")
    if args.json is not None:
        Path(args.json).write_text(
            json.dumps(report.to_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        sys.stderr.write(f"upgradelens: wrote S8 ablation JSON to {args.json}\n")
    return EXIT_OK


def _eval_replay_command(args: argparse.Namespace) -> int:
    """S8 replay comparison: run against recorded live model responses."""
    systems = tuple(args.systems) if args.systems else SYSTEMS
    try:
        cases = load_cases(Path(args.cases))
        report = run_comparison_replay(cases, Path(args.replay_dir), systems=systems)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"upgradelens: {exc}\n")
        return EXIT_INVALID_REQUEST

    markdown = report.to_markdown()
    _emit_text(markdown)

    if args.out is not None:
        Path(args.out).write_text(markdown, encoding="utf-8")
        sys.stderr.write(f"upgradelens: wrote S8 replay report to {args.out}\n")
    if args.json is not None:
        Path(args.json).write_text(
            json.dumps(report.to_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        sys.stderr.write(f"upgradelens: wrote S8 replay JSON to {args.json}\n")
    return EXIT_OK


def _retrieval_baseline_command(args: argparse.Namespace) -> int:
    """Build the SQLite index from built-in fixtures and record the baseline."""
    try:
        cases_dir = Path(args.cases_dir) if args.cases_dir else DEFAULT_RETRIEVAL_CASES_DIR
        cases = load_retrieval_cases(cases_dir)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"upgradelens: {exc}\n")
        return EXIT_INVALID_REQUEST

    db_path = args.db or tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    engine = engine_for(db_path)
    init_db(engine)
    session = session_for(engine)()
    try:
        skills = builtin_registry().all()
        report = run_baseline(session, skills, cases, top_k=args.top_k)
    finally:
        session.close()
        if not args.db:
            try:
                os.remove(db_path)
            except OSError:
                pass

    if args.out:
        Path(args.out).write_text(report.model_dump_json(indent=2), encoding="utf-8")
        sys.stderr.write(f"upgradelens: wrote retrieval baseline to {args.out}\n")

    if args.format == "md":
        _emit_text(render_retrieval_baseline_markdown(report))
    else:
        _emit(report.model_dump())
    return EXIT_OK


def _ingest_docs_command(args: argparse.Namespace) -> int:
    """Ingest documentation into the shared corpus (stage 4 / S6).

    ``--manifest`` is the supported path: it needs no Skill Pack, so adding a
    dependency to the corpus is a data change. ``--skill`` remains only to keep
    the built-in Skills ingestable while they are migrated.
    """
    if (args.manifest is None) == (args.skill is None):
        sys.stderr.write(
            "upgradelens: ingest-docs requires exactly one of "
            "--manifest (preferred) or --skill (deprecated)\n"
        )
        return EXIT_INVALID_REQUEST

    skill = None
    if args.skill is not None:
        skill = builtin_registry().get(args.skill)
        if skill is None:
            sys.stderr.write(f"upgradelens: unknown skill '{args.skill}'\n")
            return EXIT_INVALID_REQUEST
        sys.stderr.write(
            "upgradelens: --skill is deprecated; declare a source manifest and use --manifest\n"
        )

    engine = engine_for(args.db)
    init_db(engine)
    session = session_for(engine)()
    try:
        if skill is not None:
            records = ingest_skill(session, skill)
        else:
            try:
                records = ingest_corpus(session, args.manifest)
            except DocSourceManifestError as exc:
                sys.stderr.write(f"upgradelens: {exc}\n")
                return EXIT_INVALID_REQUEST
        _emit([rec.model_dump(mode="json") for rec in records])
    finally:
        session.close()
    return EXIT_OK


def _gate_command(args: argparse.Namespace) -> int:
    """Apply the CI gate to a verified report (ROADMAP 6.1).

    Reads the artifact produced by ``assess --format json`` (or ``assess --raw``)
    and exits non-zero when a ``VERIFIED`` risk meets or exceeds the blocking
    severity. Degraded / unverified findings never block.
    """
    path: Path = args.report
    if not path.is_file():
        sys.stderr.write(f"upgradelens: report not found: {path}\n")
        return EXIT_INVALID_REQUEST
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"upgradelens: cannot read report {path}: {exc}\n")
        return EXIT_INVALID_REQUEST

    if isinstance(raw, dict) and "verified" in raw:
        raw = raw["verified"]
    try:
        report = VerifiedReport.model_validate(raw)
    except ValidationError as exc:
        sys.stderr.write(f"upgradelens: invalid verified report: {exc}\n")
        return EXIT_INVALID_REQUEST

    block_on = [s for s in args.block_on.split(",") if s] if args.block_on else None
    result = gate_report(report, block_on=block_on)

    if args.format == "json":
        _emit(result.to_dict())
    else:
        sys.stdout.write(result.summary + "\n")
    return EXIT_GATE_BLOCKED if result.block else EXIT_OK


def _rag_worker_command(args: argparse.Namespace) -> int:
    """Drain pending S17 corpus backfill jobs into the shared corpus."""
    from upgradelens.docs.worker import process_pending_jobs

    init_db(args.db)
    engine = engine_for(args.db)
    network = args.network
    if args.loop:
        processed_any = False
        while True:
            counts = process_pending_jobs(engine, limit=args.limit, network=network, embedding=None)
            total = sum(counts.values())
            if total == 0:
                print("rag-worker: queue empty", file=sys.stderr)
                break
            processed_any = True
            print(f"rag-worker: processed {total} job(s): {counts}", file=sys.stderr)
            import time

            time.sleep(args.interval)
        if not processed_any:
            print("rag-worker: nothing to do", file=sys.stderr)
        return EXIT_OK
    counts = process_pending_jobs(engine, limit=args.limit, network=network, embedding=None)
    total = sum(counts.values())
    print(f"rag-worker: processed {total} job(s): {counts}", file=sys.stderr)
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

    if args.command == _LIST_CAPABILITIES_COMMAND:
        if args.base_dir is not None:
            registry = SkillRegistry.from_directory(args.base_dir)
        else:
            registry = builtin_registry()
        caps = CapabilityRegistry.from_skills(registry.all())
        _emit({"capabilities": caps.catalog()})
        return EXIT_OK

    if args.command == _RESOLVE_CAPABILITY_COMMAND:
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
        if selection is None:
            _emit({"dependency": args.dependency, "capability_id": None})
            return EXIT_OK
        resolved = builtin_registry().get(selection.skill_id)
        if resolved is None:
            _emit({"dependency": args.dependency, "capability_id": None})
            return EXIT_OK
        pack = TransformationPack.from_skill(resolved)
        _emit(
            {
                "dependency": args.dependency,
                "capability_id": pack.id,
                "allow_patch_draft": pack.allow_patch_draft(),
                "patch_rules": [r.id for r in pack.patch_rules()],
            }
        )
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
        return _ingest_docs_command(args)

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

    if args.command == _GATE_COMMAND:
        return _gate_command(args)

    if args.command == _ASSESS_COMMAND:
        return _assess_command(args)

    if args.command == _COMMENT_PR_COMMAND:
        return _comment_pr_command(args)

    if args.command == _AGENT_COMMAND:
        return _agent_command(args)

    if args.command == _SEED_REPLAY_COMMAND:
        return _seed_replay_command(args)

    if args.command == _LLM_CHECK_COMMAND:
        return _llm_check_command(args)

    if args.command == _FETCH_DOCS_COMMAND:
        return _fetch_docs_command(args)

    if args.command == _MCP_COMMAND:
        from upgradelens.mcp.server import mcp as _mcp_server

        _mcp_server.run(transport=args.transport)
        return EXIT_OK

    if args.command == _EVAL_COMMAND:
        return _eval_command(args)

    if args.command == _EVAL_COMPARE_COMMAND:
        return _eval_compare_command(args)

    if args.command == _EVAL_ABLATE_COMMAND:
        return _eval_ablate_command(args)

    if args.command == _EVAL_REPLAY_COMMAND:
        return _eval_replay_command(args)

    if args.command == _RETRIEVAL_BASELINE_COMMAND:
        return _retrieval_baseline_command(args)

    if args.command == _RAG_WORKER_COMMAND:
        return _rag_worker_command(args)

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
