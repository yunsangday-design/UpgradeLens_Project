"""Offline, reproducible evaluation harness for UpgradeLens."""

from upgradelens.eval.baselines import BASELINES, build_artifacts, run_baseline
from upgradelens.eval.cases import EvalCase, Expectation, load_case, load_cases
from upgradelens.eval.runner import (
    BaselineDelta,
    ComparisonResult,
    EvaluationResult,
    compare_runs,
    render_compare_markdown,
    render_summary_markdown,
    run_evaluation,
)
from upgradelens.eval.scorer import BaselineSummary, CaseScore, score_case, summarise

__all__ = [
    "BASELINES",
    "BaselineDelta",
    "BaselineSummary",
    "CaseScore",
    "ComparisonResult",
    "EvalCase",
    "EvaluationResult",
    "Expectation",
    "build_artifacts",
    "compare_runs",
    "load_case",
    "load_cases",
    "render_compare_markdown",
    "render_summary_markdown",
    "run_baseline",
    "run_evaluation",
    "score_case",
    "summarise",
]
