"""Security review tool surface (plan stage S7).

These are the canonical tools a security-review capability may call. They are thin,
offline wrappers around the deterministic analyzers -- no network, no model. Register
them into a :class:`~upgradelens.tools.registry.ToolRegistry` together with a
:class:`~upgradelens.core.capability.CapabilityRegistry` to exercise the S1 capability
tool-gate; the live review pipeline (``review_security``) calls the analyzers directly
and is independent of this surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from upgradelens.change.diff import parse_unified_diff
from upgradelens.core.finding import Finding
from upgradelens.core.security import SecurityFinding
from upgradelens.integrations.semgrep import SemgrepResult
from upgradelens.tools.registry import Tool

from .analyzers import (
    build_repository_context,
    check_dependency_cves,
    run_semgrep_scan,
)
from .verifiers import security_review_verifier

__all__ = ["security_review_tools", "SECURITY_REVIEW_TOOL_NAMES"]


class LoadChangeSetInput(BaseModel):
    unified_diff: str = Field(description="A unified diff to parse.")


class BuildRepoContextInput(BaseModel):
    repo_root: str = Field(description="Path to the repository root.")


class SemgrepScanInput(BaseModel):
    repo_root: str = Field(description="Path to the repository root.")
    fake: bool = Field(default=True, description="Use the deterministic regex scanner (offline).")


class DependencyCveCheckInput(BaseModel):
    repo_root: str = Field(description="Path to the repository root.")
    dependency: str = Field(description="Dependency name (any casing).")
    target_version: str | None = Field(
        default=None, description="Optional target version to check."
    )


class VerifyFindingsInput(BaseModel):
    findings: list[Finding] = Field(description="Findings to verify.")
    unified_diff: str = Field(description="The original unified diff.")


def _load_change_set(args: LoadChangeSetInput, ctx: Any) -> dict[str, Any]:
    change_set = parse_unified_diff(args.unified_diff)
    return {
        "files_changed": change_set.stat.files_changed,
        "additions": change_set.stat.additions,
        "deletions": change_set.stat.deletions,
        "files": [f.path for f in change_set.files],
    }


def _build_repo_context(args: BuildRepoContextInput, ctx: Any) -> dict[str, Any]:
    profile = build_repository_context(args.repo_root)
    return {
        "languages": [lang.language for lang in profile.languages],
        "manifests": [m.path for m in profile.manifests],
        "test_paths": list(profile.tests.test_paths),
        "symbol_count": len(profile.symbols),
    }


def _semgrep_scan(args: SemgrepScanInput, ctx: Any) -> dict[str, Any]:
    result: SemgrepResult = run_semgrep_scan(args.repo_root, fake=args.fake)
    return {
        "used_fake": result.used_fake,
        "findings": [f.model_dump(mode="json") for f in result.findings],
    }


def _dependency_cve_check(args: DependencyCveCheckInput, ctx: Any) -> dict[str, Any]:
    findings: list[SecurityFinding] = check_dependency_cves(
        args.repo_root, args.dependency, args.target_version
    )
    return {"findings": [f.model_dump(mode="json") for f in findings]}


def _verify_findings(args: VerifyFindingsInput, ctx: Any) -> dict[str, Any]:
    change_set = parse_unified_diff(args.unified_diff)
    result = security_review_verifier(args.findings, change_set)
    return {
        "passed": result.passed,
        "summary": result.summary,
        "checks": [c.model_dump(mode="json") for c in result.checks],
    }


SECURITY_REVIEW_TOOL_NAMES: tuple[str, ...] = (
    "load_change_set",
    "build_repository_context",
    "semgrep_scan",
    "dependency_cve_check",
    "verify_findings",
)


def security_review_tools() -> list[Tool]:
    """The five deterministic security-review tools."""
    return [
        Tool(
            name="load_change_set",
            description="Parse a unified diff into a ChangeSet.",
            input_model=LoadChangeSetInput,
            handler=_load_change_set,
        ),
        Tool(
            name="build_repository_context",
            description="Static-scan a repository into a profile.",
            input_model=BuildRepoContextInput,
            handler=_build_repo_context,
        ),
        Tool(
            name="semgrep_scan",
            description="Run semgrep (or the deterministic fake scanner).",
            input_model=SemgrepScanInput,
            handler=_semgrep_scan,
        ),
        Tool(
            name="dependency_cve_check",
            description="Check a dependency against the internal CVE table.",
            input_model=DependencyCveCheckInput,
            handler=_dependency_cve_check,
        ),
        Tool(
            name="verify_findings",
            description="Verify security findings cite real changed code.",
            input_model=VerifyFindingsInput,
            handler=_verify_findings,
        ),
    ]
