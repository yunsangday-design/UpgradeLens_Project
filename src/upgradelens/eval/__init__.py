"""Offline, reproducible evaluation harness for UpgradeLens."""

from upgradelens.eval.baselines import BASELINES, build_artifacts, run_baseline
from upgradelens.eval.cases import EvalCase, Expectation, load_case, load_cases
from upgradelens.eval.runner import EvaluationResult, render_summary_markdown, run_evaluation
from upgradelens.eval.scorer import BaselineSummary, CaseScore, score_case, summarise

__all__ = [
    "BASELINES",
    "BaselineSummary",
    "CaseScore",
    "EvalCase",
    "EvaluationResult",
    "Expectation",
    "build_artifacts",
    "load_case",
    "load_cases",
    "render_summary_markdown",
    "run_baseline",
    "run_evaluation",
    "score_case",
    "summarise",
]
