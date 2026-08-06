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
from upgradelens.graph import AssessmentSpec, retrieve_skill_evidence, run_assessment
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import build_bundle
from upgradelens.report import render_markdown
from upgradelens.skills import SkillParseError, SkillRegistry, builtin_registry
from upgradelens.verify import verify_report
from upgradelens.verify.version_match import extract_version

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_INVALID_REQUEST = 1
EXIT_USAGE = 2

_SCAN_COMMAND = "scan-dependency"
_SCAN_CODE_COMMAND = "scan-code"
_LIST_SKILLS_COMMAND = "list-skills"
_RESOLVE_SKILL_COMMAND = "resolve-skill"
_INGEST_DOCS_COMMAND = "ingest-docs"
_RETRIEVE_DOCS_COMMAND = "retrieve-docs"
_ASSESS_COMMAND = "assess"
_EVAL_COMMAND = "eval"

#: Shipped Core fixtures, resolved relative to the installed package so the
#: command works from any working directory.
DEFAULT_CASES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval"


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
    assess.add_argument("--repo", required=True, type=Path, help="Repository root to scan.")
    assess.add_argument("--dependency", required=True, help="Dependency name (any casing).")
    assess.add_argument(
        "--target-version",
        default=None,
        help="Target version spec (defaults to the resolved skill's target).",
    )
    assess.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional SQLite database with ingested docs for documentation evidence.",
    )
    assess.add_argument(
        "--source-id",
        default=None,
        help="Optional documentation source id to scope retrieval (defaults to the skill id).",
    )
    assess.add_argument(
        "--mode",
        default=None,
        choices=["fake", "replay", "live"],
        help="Model gateway mode (defaults to UPGRADELENS_MODEL_MODE, then 'fake').",
    )
    assess.add_argument("--model", default=None, help="Model name (live mode).")
    assess.add_argument("--api-key", default=None, help="API key (live mode, overrides env).")
    assess.add_argument("--base-url", default=None, help="OpenAI-compatible base url (live mode).")
    assess.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="Maximum total tokens for the assessment (model calls rejected beyond it).",
    )
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


def _resolve_skill(
    registry: SkillRegistry, dependency: str, target_version_spec: str | None
) -> SkillPackage | None:
    """Find the Skill Pack that serves ``dependency``.

    ``SkillRegistry.get`` is keyed by *skill id*, not by package name, so it can
    never resolve a dependency name. We try proper version-aware selection first
    and fall back to a package-name match when the target version is missing or
    not PEP 440 parseable (e.g. a raw spec like ``">=2.0"``).
    """
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


def _assess_command(args: argparse.Namespace) -> int:
    """Run the stage 5 closed loop, verify it, and print the report."""
    settings = Settings()
    registry = builtin_registry()
    code_report = scan_code_evidence(args.repo, args.dependency)

    skill = _resolve_skill(registry, args.dependency, args.target_version)

    session = None
    if args.db is not None:
        engine = engine_for(args.db)
        init_db(engine)
        session = session_for(engine)()

    degradations: list[str] = []
    try:
        doc_evidences = (
            retrieve_skill_evidence(session, skill, source_id=args.source_id)
            if (session is not None and skill is not None)
            else []
        )
        if session is None:
            degradations.append(
                "No documentation index was provided (--db); "
                "risks cannot reach 'verified' without doc evidence."
            )
        if skill is None:
            degradations.append(
                f"No Skill Pack matched '{args.dependency}'; "
                "severity rules fall back to generic scoring."
            )
        bundle = build_bundle(code_report, doc_evidences, dependency=args.dependency)
        target_version = args.target_version or (skill.target_version_spec if skill else "") or ""
        source_version = getattr(code_report, "version", "") or ""
        spec = AssessmentSpec(
            repo=str(args.repo),
            dependency=args.dependency,
            target_version_spec=target_version,
            source_version_spec=source_version,
        )
        config = _build_model_config(args, settings)
        gateway = ModelGateway(config)
        report = run_assessment(spec, bundle, gateway, skill=skill)
    finally:
        if session is not None:
            session.close()

    if args.raw:
        _emit(report)
        return EXIT_OK

    verified = verify_report(
        report,
        repo_root=Path(args.repo),
        bundle=bundle,
        code_report=code_report,
        skill=skill,
        degradations=degradations,
    )
    if args.format == "md":
        _emit_text(render_markdown(verified))
    else:
        _emit(verified)
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
