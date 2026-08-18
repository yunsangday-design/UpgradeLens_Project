"""Cross-capability gold-set evaluation (research report M1c).

Builds a small gold set for the four non-upgrade capabilities and runs each case
through the unified :func:`run_capability` dispatcher in ``fake`` mode, then scores
the resulting :class:`CapabilityRunResult` against hand-written expectations.

This reuses the S8 evaluation *philosophy* (verification gate, finding presence,
and a no-hallucination proxy) but applies it uniformly to the capability result
contract instead of the upgrade-specific ``VerifiedReport``. The S8 three-system
comparison (``direct_llm`` / ``fixed_pipeline`` / ``agent``) is upgrade-only and is
not reused here; instead we produce a *cross-capability scoreboard* that aggregates
pass rate, finding volume, verification pass and no-hallucination per capability so
the five capabilities can be compared on one normalized axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from upgradelens.capabilities.workbench import run_capability
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_DEFAULT_REPO = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "fixtures"
    / "eval"
    / "capabilities"
    / "repo"
)
_GOLD_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "fixtures"
    / "eval"
    / "capabilities"
    / "gold_cases.yaml"
)


class CapabilityGoldExpect(BaseModel):
    min_findings: int = 1
    must_include_severities: list[str] = Field(default_factory=list)
    must_include_keywords: list[str] = Field(default_factory=list)
    verification_passed: bool | None = None


class CapabilityGoldCase(BaseModel):
    name: str
    kind: str
    inputs: dict[str, str] = Field(default_factory=dict)
    expect: CapabilityGoldExpect = Field(default_factory=CapabilityGoldExpect)


@dataclass
class CaseScore:
    name: str
    kind: str
    passed: bool
    n_findings: int
    verified_findings: int
    verification_passed: bool
    hallucination_free: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class CapabilityEvalReport:
    mode: str
    repo: str
    per_kind: dict[str, dict[str, Any]]
    total_cases: int
    total_passed: int
    overall_pass_rate: float
    hallucination_free_rate: float
    cases: list[CaseScore]
    scoreboard_md: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "repo": self.repo,
            "per_kind": self.per_kind,
            "total_cases": self.total_cases,
            "total_passed": self.total_passed,
            "overall_pass_rate": self.overall_pass_rate,
            "hallucination_free_rate": self.hallucination_free_rate,
            "scoreboard_md": self.scoreboard_md,
        }


def load_gold_cases(path: str | Path | None = None) -> list[CapabilityGoldCase]:
    """Load the capability gold set from a YAML file (defaults to the bundled one)."""
    p = Path(path) if path else _GOLD_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    return [CapabilityGoldCase.model_validate(c) for c in data]


def _build_task(case: CapabilityGoldCase, repo: Path) -> SoftwareTask:
    inputs = case.inputs
    return SoftwareTask(
        task_id=f"gold-{case.name}",
        kind=TaskKind(case.kind),
        goal=inputs.get("goal", case.name),
        context=TaskContext(
            repo=str(repo),
            dependency=inputs.get("dependency", ""),
            unified_diff=inputs.get("unified_diff", ""),
            issue_text=inputs.get("issue_text", ""),
            from_version=inputs.get("from_version", ""),
            to_version=inputs.get("to_version", ""),
        ),
    )


def score_case(
    case: CapabilityGoldCase, *, mode: str = "fake", repo: str | Path | None = None
) -> CaseScore:
    """Run one gold case through the unified dispatcher and score the result."""
    repo_path = Path(repo) if repo else _DEFAULT_REPO
    task = _build_task(case, repo_path)
    result = run_capability(task, mode=mode)

    findings = result.findings or []
    n = len(findings)
    verified = sum(1 for f in findings if str(f.get("status")) == "verified")
    verif = result.verification or {}
    verification_passed = bool(verif.get("passed"))

    exp = case.expect
    reasons: list[str] = []
    passed = True

    if n < exp.min_findings:
        passed = False
        reasons.append(f"findings {n} < 期望 {exp.min_findings}")
    else:
        reasons.append(f"findings {n} ≥ 期望 {exp.min_findings}")

    if exp.must_include_severities:
        present = {str(f.get("severity", "")).lower() for f in findings}
        missing = [s for s in exp.must_include_severities if s.lower() not in present]
        if missing:
            passed = False
            reasons.append(f"缺少严重度 {missing}")
        else:
            reasons.append("严重度满足")

    if exp.must_include_keywords:
        blob = " ".join(
            " ".join(
                str(x)
                for x in (
                    f.get("summary"),
                    f.get("detail"),
                    f.get("title"),
                    f.get("category"),
                    f.get("severity"),
                )
            )
            for f in findings
        ).lower()
        missing = [k for k in exp.must_include_keywords if k.lower() not in blob]
        if missing:
            passed = False
            reasons.append(f"缺少关键字 {missing}")
        else:
            reasons.append("关键字满足")

    if exp.verification_passed is not None:
        if verification_passed != exp.verification_passed:
            passed = False
            reasons.append(f"验证 passed={verification_passed} ≠ 期望 {exp.verification_passed}")
        else:
            reasons.append("验证满足")

    hallucination_free = all(
        bool(f.get("evidence_ids"))
        for f in findings
        if str(f.get("status")) == "verified"
        or str(f.get("severity", "")).lower() in ("high", "critical")
    )

    return CaseScore(
        name=case.name,
        kind=case.kind,
        passed=passed,
        n_findings=n,
        verified_findings=verified,
        verification_passed=verification_passed,
        hallucination_free=hallucination_free,
        reasons=reasons,
    )


def _render_scoreboard(
    per_kind: dict[str, dict[str, Any]], total: int, total_passed: int, hf: int
) -> str:
    lines = [
        "# Capability Gold-Set Evaluation",
        "",
        f"Overall: **{total_passed}/{total}** cases passed | no-hallucination: **{hf}/{total}**",
        "",
        "| Capability | Cases | Passed | Pass% | Findings | Verified | "
        "Verification% | Halluc-free% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for kind, k in per_kind.items():
        n = k["n_cases"]
        p = k["n_passed"]
        vpct = round(100 * k["verification_pass"] / n, 1) if n else 0.0
        hpct = round(100 * k["hallucination_free"] / n, 1) if n else 0.0
        ppct = round(100 * p / n, 1) if n else 0.0
        lines.append(
            f"| {kind} | {n} | {p} | {ppct}% | {k['total_findings']} | "
            f"{k['verified_findings']} | {vpct}% | {hpct}% |"
        )
    return "\n".join(lines)


def run_capability_eval(
    *,
    mode: str = "fake",
    repo: str | Path | None = None,
    gold_path: str | Path | None = None,
    cases: list[CapabilityGoldCase] | None = None,
) -> CapabilityEvalReport:
    """Run every capability gold case and aggregate a cross-capability scoreboard."""
    cases_in = cases if cases is not None else load_gold_cases(gold_path)
    scores = [score_case(c, mode=mode, repo=repo) for c in cases_in]

    per_kind: dict[str, dict[str, Any]] = {}
    for s in scores:
        k = per_kind.setdefault(
            s.kind,
            {
                "n_cases": 0,
                "n_passed": 0,
                "total_findings": 0,
                "verified_findings": 0,
                "verification_pass": 0,
                "hallucination_free": 0,
            },
        )
        k["n_cases"] += 1
        k["n_passed"] += int(s.passed)
        k["total_findings"] += s.n_findings
        k["verified_findings"] += s.verified_findings
        k["verification_pass"] += int(s.verification_passed)
        k["hallucination_free"] += int(s.hallucination_free)

    total = len(scores)
    total_passed = sum(int(s.passed) for s in scores)
    hf = sum(int(s.hallucination_free) for s in scores)
    scoreboard = _render_scoreboard(per_kind, total, total_passed, hf)

    return CapabilityEvalReport(
        mode=mode,
        repo=str(repo or _DEFAULT_REPO),
        per_kind=per_kind,
        total_cases=total,
        total_passed=total_passed,
        overall_pass_rate=round(total_passed / total, 3) if total else 0.0,
        hallucination_free_rate=round(hf / total, 3) if total else 0.0,
        cases=scores,
        scoreboard_md=scoreboard,
    )


if __name__ == "__main__":
    import sys

    _mode = sys.argv[1] if len(sys.argv) > 1 else "fake"
    report = run_capability_eval(mode=_mode)
    print(report.scoreboard_md)
    failed = [s for s in report.cases if not s.passed]
    if failed:
        print("\nFAILED:")
        for s in failed:
            print(f"  - {s.kind}/{s.name}: {'; '.join(s.reasons)}")
        sys.exit(1)
    print("\nAll gold cases passed.")
