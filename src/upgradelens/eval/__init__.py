"""Offline, reproducible evaluation harness for UpgradeLens."""

from upgradelens.eval.baselines import BASELINES, build_artifacts, run_baseline
from upgradelens.eval.cases import EvalCase, Expectation, load_case, load_cases
from upgradelens.eval.comparison import (
    SYSTEMS,
    ComparisonReport,
    ComparisonRun,
    S8Metrics,
    compute_metrics,
    run_agent_system,
    run_comparison,
    run_comparison_from_dir,
    run_direct_llm,
    run_fixed_pipeline,
)
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
    "ComparisonReport",
    "ComparisonRun",
    "ComparisonResult",
    "EvalCase",
    "EvaluationResult",
    "Expectation",
    "S8Metrics",
    "SYSTEMS",
    "build_artifacts",
    "compare_runs",
    "compute_metrics",
    "load_case",
    "load_cases",
    "render_compare_markdown",
    "render_summary_markdown",
    "run_agent_system",
    "run_baseline",
    "run_comparison",
    "run_comparison_from_dir",
    "run_direct_llm",
    "run_evaluation",
    "run_fixed_pipeline",
    "score_case",
    "summarise",
]
