"""PR review tool surface (plan stage S4).

These are the seven canonical tools a PR review capability may call. They are
thin, offline wrappers around the deterministic analyzers -- no network, no model.
Register them into a :class:`~upgradelens.tools.registry.ToolRegistry` together
with a :class:`~upgradelens.core.capability.CapabilityRegistry` to exercise the
S1 capability tool-gate; the live review pipeline (``review_pull_request``) calls
the analyzers directly and is independent of this surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from upgradelens.change.diff import parse_unified_diff
from upgradelens.core.finding import Finding
from upgradelens.tools.registry import Tool

from .analyzers import (
    analyze_change_impact,
    build_repository_context,
    recommend_tests,
    retrieve_code_context,
)
from .verifiers import pr_review_verifier

__all__ = ["pr_review_tools", "PR_REVIEW_TOOL_NAMES"]


class LoadChangeSetInput(BaseModel):
    unified_diff: str = Field(description="A unified diff to parse.")


class BuildRepoContextInput(BaseModel):
    repo_root: str = Field(description="Path to the repository root.")


class AnalyzeChangeImpactInput(BaseModel):
    unified_diff: str = Field(description="A unified diff.")
    repo_root: str = Field(description="Path to the repository root.")


class RetrieveCodeContextInput(BaseModel):
    repo_root: str = Field(description="Path to the repository root.")
    paths: list[str] | None = Field(default=None, description="Optional file subset.")


class RetrieveDocsInput(BaseModel):
    query: str = Field(description="Documentation query.")
    package: str | None = Field(default=None, description="Optional package scope.")


class RecommendTestsInput(BaseModel):
    unified_diff: str = Field(description="A unified diff.")
    repo_root: str = Field(description="Path to the repository root.")


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


def _analyze_change_impact(args: AnalyzeChangeImpactInput, ctx: Any) -> dict[str, Any]:
    change_set = parse_unified_diff(args.unified_diff)
    impact = analyze_change_impact(change_set, args.repo_root)
    return {
        "direct_symbols": [s.name for s in impact.direct],
        "impacted_symbols": [s.name for s in impact.impacted],
        "labels": impact.labels,
    }


def _retrieve_code_context(args: RetrieveCodeContextInput, ctx: Any) -> dict[str, Any]:
    symbols = retrieve_code_context(args.repo_root, args.paths)
    return {"symbols": [s.name for s in symbols]}


def _retrieve_docs(args: RetrieveDocsInput, ctx: Any) -> dict[str, Any]:
    # Offline stub: real retrieval needs the docs DB. The review pipeline does not
    # depend on this tool in fake mode; it exists for capability completeness.
    return {"query": args.query, "package": args.package, "chunks": []}


def _recommend_tests(args: RecommendTestsInput, ctx: Any) -> dict[str, Any]:
    change_set = parse_unified_diff(args.unified_diff)
    profile = build_repository_context(args.repo_root)
    impact = analyze_change_impact(change_set, args.repo_root)
    tests = recommend_tests(change_set, impact, profile)
    return {"tests": [t.model_dump() for t in tests]}


def _verify_findings(args: VerifyFindingsInput, ctx: Any) -> dict[str, Any]:
    change_set = parse_unified_diff(args.unified_diff)
    result = pr_review_verifier(args.findings, change_set)
    return {
        "passed": result.passed,
        "summary": result.summary,
        "checks": [c.model_dump() for c in result.checks],
    }


PR_REVIEW_TOOL_NAMES: tuple[str, ...] = (
    "load_change_set",
    "build_repository_context",
    "analyze_change_impact",
    "retrieve_code_context",
    "retrieve_docs",
    "recommend_tests",
    "verify_findings",
)


def pr_review_tools() -> list[Tool]:
    """The seven deterministic PR review tools."""
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
            name="analyze_change_impact",
            description="One-hop impact analysis over the change set.",
            input_model=AnalyzeChangeImpactInput,
            handler=_analyze_change_impact,
        ),
        Tool(
            name="retrieve_code_context",
            description="Extract code symbols for the given files.",
            input_model=RetrieveCodeContextInput,
            handler=_retrieve_code_context,
        ),
        Tool(
            name="retrieve_docs",
            description="Retrieve documentation chunks for a query.",
            input_model=RetrieveDocsInput,
            handler=_retrieve_docs,
        ),
        Tool(
            name="recommend_tests",
            description="Recommend tests covering the changed code.",
            input_model=RecommendTestsInput,
            handler=_recommend_tests,
        ),
        Tool(
            name="verify_findings",
            description="Verify review findings cite real changed code.",
            input_model=VerifyFindingsInput,
            handler=_verify_findings,
        ),
    ]
