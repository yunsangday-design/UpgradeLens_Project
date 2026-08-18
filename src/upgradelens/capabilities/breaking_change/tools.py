"""Breaking-change tool surface (plan stage S5).

Thin, offline wrappers around the deterministic analyzers. Register them into a
:class:`~upgradelens.tools.registry.ToolRegistry` together with a
:class:`~upgradelens.core.capability.CapabilityRegistry` to exercise the S1
capability tool-gate. The live pipeline (``review_breaking_changes``) calls the
analyzers directly and is independent of this surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from upgradelens.change.diff import parse_unified_diff
from upgradelens.core.finding import Finding
from upgradelens.tools.registry import Tool

from .analyzers import (
    classify_api_change,
    compare_versions,
    detect_breaking_changes,
    extract_public_symbols,
)

__all__ = ["breaking_change_tools", "BREAKING_CHANGE_TOOL_NAMES"]


class ExtractPublicSymbolsInput(BaseModel):
    repo_root: str = Field(description="Path to the repository root.")


class LoadChangeSetInput(BaseModel):
    unified_diff: str = Field(description="A unified diff to parse.")


class CompareVersionsInput(BaseModel):
    from_version: str = Field(description="Source version.")
    to_version: str = Field(description="Target version.")


class ClassifyApiChangeInput(BaseModel):
    symbol: str = Field(description="Symbol name.")
    old_signature: str = Field(default="", description="Old signature.")
    new_signature: str = Field(default="", description="New signature.")


class DetectBreakingChangesInput(BaseModel):
    unified_diff: str = Field(description="A unified diff.")
    repo_root: str = Field(description="Path to the repository root.")


class VerifyReportInput(BaseModel):
    findings: list[Finding] = Field(description="Findings to verify.")
    unified_diff: str = Field(description="The original unified diff.")


def _extract_public_symbols(args: ExtractPublicSymbolsInput, ctx: Any) -> dict[str, Any]:
    return {"symbols": [s.name for s in extract_public_symbols(args.repo_root)]}


def _load_change_set(args: LoadChangeSetInput, ctx: Any) -> dict[str, Any]:
    return {"files_changed": len(parse_unified_diff(args.unified_diff).files)}


def _compare_versions(args: CompareVersionsInput, ctx: Any) -> dict[str, Any]:
    comp = compare_versions(args.from_version, args.to_version)
    return {"level": comp.level}


def _classify_api_change(args: ClassifyApiChangeInput, ctx: Any) -> dict[str, Any]:
    return {"kind": classify_api_change(args.symbol, args.old_signature, args.new_signature).value}


def _detect_breaking_changes(args: DetectBreakingChangesInput, ctx: Any) -> dict[str, Any]:
    change_set = parse_unified_diff(args.unified_diff)
    candidates = detect_breaking_changes(change_set, args.repo_root)
    return {"candidates": [c.model_dump() for c in candidates]}


def _verify_report(args: VerifyReportInput, ctx: Any) -> dict[str, Any]:
    from .verifiers import verify_breaking_changes

    change_set = parse_unified_diff(args.unified_diff)
    result = verify_breaking_changes(args.findings, change_set)
    return {
        "passed": result.passed,
        "summary": result.summary,
        "checks": [c.model_dump() for c in result.checks],
    }


BREAKING_CHANGE_TOOL_NAMES: tuple[str, ...] = (
    "load_change_set",
    "detect_breaking_changes",
    "extract_public_symbols",
    "classify_api_change",
    "compare_versions",
    "verify_report",
)


def breaking_change_tools() -> list[Tool]:
    """The six deterministic breaking-change tools."""
    return [
        Tool(
            name="load_change_set",
            description="Parse a unified diff into a ChangeSet.",
            input_model=LoadChangeSetInput,
            handler=_load_change_set,
        ),
        Tool(
            name="detect_breaking_changes",
            description="Pre-filter changed public symbols as candidates.",
            input_model=DetectBreakingChangesInput,
            handler=_detect_breaking_changes,
        ),
        Tool(
            name="extract_public_symbols",
            description="Extract top-level public symbols from the repo.",
            input_model=ExtractPublicSymbolsInput,
            handler=_extract_public_symbols,
        ),
        Tool(
            name="classify_api_change",
            description="Heuristically classify an API change kind.",
            input_model=ClassifyApiChangeInput,
            handler=_classify_api_change,
        ),
        Tool(
            name="compare_versions",
            description="Classify upgrade magnitude.",
            input_model=CompareVersionsInput,
            handler=_compare_versions,
        ),
        Tool(
            name="verify_report",
            description="Verify breaking-change findings cite real changed code.",
            input_model=VerifyReportInput,
            handler=_verify_report,
        ),
    ]
