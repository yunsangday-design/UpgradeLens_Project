"""Issue-repair gold-set evaluation (competitiveness plan, stage B2).

Extends the A5 cross-capability scoreboard with issue-repair specific metrics
that are meaningful offline, because the root-cause locator is deterministic:

1. ``root_cause_hit_rate`` — the keyword scan (:func:`locate_root_cause`) must
   name the expected ``file:symbol`` for every locatable case;
2. ``clarification_correct`` — an information-less report must yield
   "No symbol matched", never a fabricated verified root cause;
3. ``pipeline_ok`` — the full ``repair_issue`` step runs fake/offline and
   returns findings with a verification record;
4. ``repro_fails_before_fix`` — the bundled reproduction tests are *red*
   against the deliberately broken fixture repo (gold-set self-check; they
   must pass only after a fix).

Usage::

    from upgradelens.eval.issue_repair_eval import run_issue_repair_eval

    report = run_issue_repair_eval()           # offline, deterministic
    print(report.scoreboard_md)
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from upgradelens.capabilities.issue_repair.analyzers import (
    load_issue,
    locate_root_cause,
)
from upgradelens.capabilities.workbench import run_capability
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES = _ROOT / "tests/fixtures/eval/issue_repair/cases.yaml"
DEFAULT_REPO = _ROOT / "tests/fixtures/eval/issue_repair/repos/demo_service"


class IssueRepairGoldCase(BaseModel):
    """One B2 gold case: a bug report plus the deterministic expectation."""

    name: str
    category: str = "general"
    issue_text: str
    expected_file: str = ""
    expected_symbol: str = ""
    expect_no_match: bool = False


@dataclass(frozen=True)
class IssueRepairCaseScore:
    """Per-case outcome of the B2 gold set."""

    name: str
    category: str
    root_cause_hit: bool | None  # None for clarification cases
    no_match_correct: bool | None  # only set for clarification cases
    pipeline_ok: bool
    detail: str = ""


@dataclass
class IssueRepairEvalReport:
    """Aggregate B2 scoreboard."""

    total_cases: int = 0
    locatable_cases: int = 0
    root_cause_hits: int = 0
    clarification_cases: int = 0
    clarification_correct: int = 0
    pipeline_ok: int = 0
    repro_fails_before_fix: bool | None = None
    cases: list[IssueRepairCaseScore] = field(default_factory=list)

    @property
    def root_cause_hit_rate(self) -> float:
        return self.root_cause_hits / self.locatable_cases if self.locatable_cases else 0.0

    @property
    def pipeline_ok_rate(self) -> float:
        return self.pipeline_ok / self.total_cases if self.total_cases else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "locatable_cases": self.locatable_cases,
            "root_cause_hits": self.root_cause_hits,
            "root_cause_hit_rate": self.root_cause_hit_rate,
            "clarification_cases": self.clarification_cases,
            "clarification_correct": self.clarification_correct,
            "pipeline_ok": self.pipeline_ok,
            "pipeline_ok_rate": self.pipeline_ok_rate,
            "repro_fails_before_fix": self.repro_fails_before_fix,
            "cases": [
                {
                    "name": c.name,
                    "category": c.category,
                    "root_cause_hit": c.root_cause_hit,
                    "no_match_correct": c.no_match_correct,
                    "pipeline_ok": c.pipeline_ok,
                    "detail": c.detail,
                }
                for c in self.cases
            ],
        }

    @property
    def scoreboard_md(self) -> str:
        lines = [
            "## Issue-Repair Gold-Set Evaluation (B2)",
            "",
            "| Metric | Result |",
            "|---|---|",
            f"| Root-cause hit rate | {self.root_cause_hits}/{self.locatable_cases} "
            f"({self.root_cause_hit_rate:.0%}) |",
            f"| Clarification correctness | {self.clarification_correct}/"
            f"{self.clarification_cases} |",
            f"| Fake pipeline ok | {self.pipeline_ok}/{self.total_cases} "
            f"({self.pipeline_ok_rate:.0%}) |",
            f"| Repro tests red before fix | {self.repro_fails_before_fix} |",
            "",
            "| Case | Category | Root-cause hit | No-match correct | Pipeline |",
            "|---|---|---|---|---|",
        ]
        for c in self.cases:
            hit = "—" if c.root_cause_hit is None else ("✓" if c.root_cause_hit else "✗")
            nomatch = "—" if c.no_match_correct is None else ("✓" if c.no_match_correct else "✗")
            lines.append(
                f"| {c.name} | {c.category} | {hit} | {nomatch} | {'✓' if c.pipeline_ok else '✗'} |"
            )
        return "\n".join(lines)


def load_issue_cases(path: str | Path | None = None) -> list[IssueRepairGoldCase]:
    """Load the B2 gold cases from YAML (mirrors the A5 loader)."""
    case_path = Path(path) if path else DEFAULT_CASES
    data = yaml.safe_load(case_path.read_text(encoding="utf-8")) or []
    return [IssueRepairGoldCase.model_validate(c) for c in data]


def _repro_tests_are_red(repo: Path, timeout: int = 180) -> bool:
    """Run the bundled reproduction tests; they must fail against the broken repo."""
    test_file = repo / "tests" / "test_repro.py"
    if not test_file.is_file():
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pytest",
                str(test_file),
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode != 0


def run_issue_repair_eval(
    *,
    mode: str = "fake",
    cases_path: str | Path | None = None,
    repo: str | Path | None = None,
    run_repro: bool = True,
) -> IssueRepairEvalReport:
    """Run the B2 gold set. Offline and deterministic in the default fake mode."""
    cases = load_issue_cases(cases_path)
    repo_root = Path(repo) if repo else DEFAULT_REPO

    report = IssueRepairEvalReport(total_cases=len(cases))
    for case in cases:
        text = case.issue_text
        expect_no_match = case.expect_no_match

        issue = load_issue(text)
        located = locate_root_cause(issue, str(repo_root))

        root_hit: bool | None = None
        no_match_ok: bool | None = None
        if expect_no_match:
            no_match_ok = "No symbol matched" in located
        else:
            expected = f"{case.expected_file}:{case.expected_symbol}"
            root_hit = expected in located

        detail = ""
        try:
            task = SoftwareTask(
                task_id=f"b2-{case.name}",
                kind=TaskKind.ISSUE_REPAIR,
                goal=text,
                context=TaskContext(repo=str(repo_root), issue_text=text),
            )
            res = run_capability(task, mode=mode)
            pipeline_ok = (
                res.status == "succeeded" and bool(res.findings) and bool(res.verification)
            )
        except Exception as exc:  # noqa: BLE001 — eval must never crash
            pipeline_ok = False
            detail = f"{type(exc).__name__}: {exc}"

        report.cases.append(
            IssueRepairCaseScore(
                name=case.name,
                category=case.category,
                root_cause_hit=root_hit,
                no_match_correct=no_match_ok,
                pipeline_ok=pipeline_ok,
                detail=detail,
            )
        )
        if root_hit:
            report.root_cause_hits += 1
        if no_match_ok:
            report.clarification_correct += 1
        if pipeline_ok:
            report.pipeline_ok += 1
        if expect_no_match:
            report.clarification_cases += 1
        else:
            report.locatable_cases += 1

    if run_repro:
        report.repro_fails_before_fix = _repro_tests_are_red(repo_root)
    return report


def main_cli(**kwargs: Any) -> int:
    """Thin adapter used by the CLI ``eval-issue-repair`` command."""
    report = run_issue_repair_eval(**kwargs)
    print(report.scoreboard_md)
    return 0 if report.root_cause_hit_rate >= 1.0 else 1
