"""ROADMAP Step 3 -- the live plan drives (and records) every agent action."""

from __future__ import annotations

from pathlib import Path

from upgradelens.agent.loop import (
    _add_adhoc_step,
    _evaluate_step,
    _resolve_step,
    run_agent,
)
from upgradelens.agent.plan import SKIPPED, SUCCEEDED, AgentPlan, AgentPlanStep
from upgradelens.agent.planner import build_agent_plan
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.pipeline import AssessmentRequest
from upgradelens.tools.live_repo import is_repo_url
from upgradelens.tools.registry import ToolContext, default_registry


def _fake_gateway() -> ModelGateway:
    return ModelGateway(ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""))


def _request(repo: str, db: str | None = None):
    return AssessmentRequest(repo=repo, dependency="pydantic", target_version="2.0", db=db)


def test_plan_step_status_transitions() -> None:
    step = AgentPlanStep(id="s1", tool="scan_code", seq=1)
    assert step.status == "pending"
    assert step.attempt == 0
    step.mark_running()
    assert step.status == "running"
    assert step.attempt == 1
    step.mark_outcome(True, "scanned 3 usages")
    assert step.status == SUCCEEDED
    assert step.observation == "scanned 3 usages"
    # A second run on a retryable step counts attempts.
    step.mark_running()
    assert step.attempt == 2


def test_evaluate_step_skips_inapplicable_tools() -> None:
    class _Acc:
        repo_path: Path | None = None

    local = _Acc()
    url = _Acc()
    req_no_db = _request("/tmp/repo", db=None)
    req_db = _request("/tmp/repo", db="store.db")

    # Local repo: clone_repo is skipped, doc retrieval skipped without a db.
    assert _evaluate_step("clone_repo", local, req_no_db, False) == SKIPPED
    assert _evaluate_step("retrieve_for_package", local, req_no_db, False) == SKIPPED
    # With a db present it becomes runnable (only after a checkout exists).
    assert _evaluate_step("retrieve_for_package", local, req_db, False) == "wait"
    # URL repo: clone_repo is runnable; scan needs the checkout first.
    assert _evaluate_step("clone_repo", url, req_db, True) == "run"
    assert _evaluate_step("scan_code", url, req_db, True) == "wait"


def test_resolve_step_finds_pending_else_adhoc() -> None:
    plan = AgentPlan(
        steps=[
            AgentPlanStep(id="s1", tool="scan_dependency", seq=1, status="succeeded"),
            AgentPlanStep(id="s2", tool="scan_code", seq=2, status="pending"),
        ]
    )
    assert _resolve_step(plan, "scan_code") is plan.steps[1]
    # A tool not in the plan resolves to None -> the loop records it as ad-hoc.
    assert _resolve_step(plan, "retrieve_for_package") is None
    adhoc = _add_adhoc_step(plan, "retrieve_for_package", "model asked for docs")
    assert adhoc.id.startswith("a")
    assert adhoc.reason == "model asked for docs"
    assert adhoc in plan.steps


def test_fake_run_writes_plan_back_and_records_step_ids() -> None:
    """Fake mode drives the same state machine and persists plan state."""
    repo_dir = Path("/tmp") / "s3-plan-test"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["pydantic>=1.10"]\n'
    )
    request = _request(str(repo_dir), db=None)
    plan = build_agent_plan(
        gateway=_fake_gateway(),
        registry=default_registry(),
        repo=str(repo_dir),
        dependency="pydantic",
        target_version="2.0",
        repo_is_url=is_repo_url(str(repo_dir)),
    )
    written: list[AgentPlan] = []
    with ToolContext() as ctx:
        result = run_agent(
            request,
            _fake_gateway(),
            ctx,
            registry=default_registry(),
            plan=plan,
            plan_writer=lambda p: written.append(p),
        )
    # Steps are all resolved; no db -> doc retrieval is skipped, not failed.
    assert plan.is_resolved()
    assert {s.tool: s.status for s in plan.steps} == {
        "scan_dependency": "succeeded",
        "scan_code": "succeeded",
        "retrieve_for_package": "skipped",
        "supplement_retrieval": "skipped",
    }
    # The loop wrote the plan back at least once during execution.
    assert written
    # Each trace event carries the owning plan step id.
    assert {e.plan_step_id for e in ctx.trace.events} <= {s.id for s in plan.steps}
    assert all(e.plan_step_id for e in ctx.trace.events)
    assert result.verified is not None
