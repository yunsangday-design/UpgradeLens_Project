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
  target version (generic fallback when nothing matches).

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
from upgradelens.domain import (
    DependencyAnalysisRequest,
    DependencyScanResult,
    IssueCode,
    ParseIssue,
    ResolutionStatus,
)
from upgradelens.skills import SkillParseError, SkillRegistry, builtin_registry

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_INVALID_REQUEST = 1
EXIT_USAGE = 2

_SCAN_COMMAND = "scan-dependency"
_SCAN_CODE_COMMAND = "scan-code"
_LIST_SKILLS_COMMAND = "list-skills"
_RESOLVE_SKILL_COMMAND = "resolve-skill"


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


def _emit(result: BaseModel) -> None:
    """Write the result as UTF-8 JSON, independent of console encoding."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    payload = json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False)
    sys.stdout.write(payload + "\n")


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
        _emit(scan_code_evidence(args.repo, args.dependency))
        return EXIT_OK

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
