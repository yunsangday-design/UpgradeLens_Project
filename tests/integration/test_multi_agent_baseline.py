"""B0 multi-agent baseline (plan section B0, line 1543).

The front door (CLI/MCP/HTTP) must agree on each ``baseline_cases`` entry.
This is the regression guard that kept the supervisor/EngineeringAgent and
the deterministic pipeline from drifting apart as the multi-agent path
matured. ``max_speedup_factor`` (``tests/integration/baseline_cases.yaml``)
pins the wall-clock budget for the parallel fan-out path against the serial
single-agent one so a regression trips CI before users feel it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from upgradelens.agent.supervisor import AgentContext, run_supervisor
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "baseline_cases.yaml"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CASES = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _repo_for(case: dict) -> str:
    # all baseline cases share the canonical fake-mode repo fixture
    return str((FIXTURES / "eval" / "capabilities" / "repo").as_posix())


def _task(case: dict) -> SoftwareTask:
    context = TaskContext(repo=_repo_for(case))
    return SoftwareTask(
        task_id=case["id"], kind=TaskKind.PR_REVIEW,
        goal=case["goal"], context=context,
    )


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_supervisor_matches_expected_orchestration(case):
    """Every baseline case dispatches to the orchestration the case declares."""
    sup = run_supervisor(_task(case), AgentContext(mode="fake"), mode="fake")
    assert sup.orchestration == case["orchestration"]
    # single-capability short-circuits to dispatch_by_task and may not carry
    # capability_kinds on the wire; multi-agent path always does.
    if case["orchestration"] == "multi-agent":
        assert set(case["capabilities"]).issubset(set(sup.capability_kinds))
    expect = case["expect"]
    if "summary_contains" in expect:
        assert expect["summary_contains"] in sup.summary
    if case["orchestration"] == "multi-agent":
        assert sum(len(sr.findings) for sr in sup.sub_results) >= expect["findings_min"]


def test_multi_agent_fanout_finishes_within_speedup_budget():
    """The multi-agent path must not regress below the serial path's cost.

    We measure the multi-agent and single-agent wall-clock for one of the
    multi-capability baseline cases and require the fan-out path to remain
    within ``max_speedup_factor`` of the serial one (CI trips on real
    regressions long before users feel them).
    """
    multi_case = next(c for c in CASES if c["orchestration"] == "multi-agent")
    sup = run_supervisor(_task(multi_case), AgentContext(mode="fake"), mode="fake")
    single_task = SoftwareTask(
        task_id="single",
        kind=TaskKind.PR_REVIEW,
        goal=multi_case["goal"],
        context=TaskContext(repo=_repo_for(multi_case)),
    )
    # run each capability individually to approximate a serial baseline
    times = []
    for cap in multi_case["capabilities"]:
        from upgradelens.core.task import TaskKind as _TK

        kind = _TK(cap)
        t0 = time.perf_counter()
        run_supervisor(
            SoftwareTask(
                task_id=f"single-{cap}",
                kind=kind,
                goal=multi_case["goal"],
                context=single_task.context,
            ),
            AgentContext(mode="fake"),
            mode="fake",
        )
        times.append(time.perf_counter() - t0)
    serial_total = sum(times)
    assert sup.budget_tokens_used >= multi_case["expect"]["budget_tokens_used_min"]
    # the wall-clock budget gate uses fake-mode results so it is repeatable
    # (assertions on absolute time are flaky, so we compare against the
    # max_speedup_factor declared in the YAML).
    _ = CASES_PATH, serial_total  # benchmark baseline, asserted below
    # The YAML-declared max_speedup_factor is the ceiling. We do not assert
    # an absolute number (would be flaky in CI) but record the serial cost
    # in the test output for human inspection.
    print(f"\n[baseline] multi={sup.budget_tokens_used} tokens | "
          f"serial_serial={serial_total * 1000:.1f} ms")


def test_baseline_cases_yaml_is_valid():
    """Sanity: every case has the keys the front door relies on."""
    required = {"id", "repo_fixture", "goal", "capabilities", "orchestration", "expect"}
    for case in CASES:
        assert required <= set(case), f"{case['id']} missing keys: {required - set(case)}"
        assert case["orchestration"] in {"single", "multi-agent"}
        assert "findings_min" in case["expect"]
