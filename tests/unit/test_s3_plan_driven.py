"""ROADMAP Step 3 -- the live plan drives (and records) every agent action."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upgradelens.agent import loop as _loop
from upgradelens.agent.loop import (
    ToolCallDecision,
    _Accumulator,
    _add_adhoc_step,
    _already_collected,
    _evaluate_step,
    _resolve_step,
    _run_driven,
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

    # Local repo: clone_repo is skipped.
    assert _evaluate_step("clone_repo", local, req_no_db, False) == SKIPPED
    # S16: retrieve_for_package no longer requires a db (online fallback).
    # It returns "wait" because repo_path is None; would return "run" if set.
    assert _evaluate_step("retrieve_for_package", local, req_no_db, False) == "wait"
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
    # Steps are all resolved; S16: retrieve_for_package runs even without a db
    # (online fallback available).  In fake mode it succeeds but returns empty.
    assert plan.is_resolved()
    assert {s.tool: s.status for s in plan.steps} == {
        "scan_dependency": "succeeded",
        "scan_code": "succeeded",
        "retrieve_for_package": "succeeded",
        "supplement_retrieval": "skipped",
    }
    # The loop wrote the plan back at least once during execution.
    assert written
    # Each trace event carries the owning plan step id.
    assert {e.plan_step_id for e in ctx.trace.events} <= {s.id for s in plan.steps}
    assert all(e.plan_step_id for e in ctx.trace.events)
    assert result.verified is not None


def test_already_collected_dedups_covering_tools() -> None:
    acc = _Accumulator()
    assert not _already_collected("clone_repo", acc)
    assert not _already_collected("scan_dependency", acc)
    assert not _already_collected("scan_code", acc)
    # retrieve_for_package accumulates chunks across queries; never deduped.
    assert not _already_collected("retrieve_for_package", acc)
    acc.repo_path = Path("/tmp/demo")
    assert _already_collected("clone_repo", acc)
    acc.scan_result = object()  # type: ignore[assignment]  # presence check only
    assert _already_collected("scan_dependency", acc)
    assert not _already_collected("scan_code", acc)


def test_live_loop_dedups_redundant_scan_calls(monkeypatch: Any) -> None:
    """Live mode: re-calling a completed tool is skipped; consecutive hits force convergence."""
    repo_dir = Path("/tmp") / "s3-dedup-test"
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
    gateway = ModelGateway(
        ModelConfig(model="live", mode=ModelMode.LIVE, api_key="", base_url="")
    )
    # The model insists on calling scan_dependency even after it succeeded.
    # After the first real run + 2 redundant hits, the loop forces convergence.
    decisions = iter(
        [
            ToolCallDecision(tool="scan_dependency", done=False, thought="first scan"),
            # After scan_dependency succeeds, _decide filters it from specs.
            # But the stub ignores the prompt and returns it anyway:
            ToolCallDecision(tool="scan_dependency", done=False, thought="redundant 1"),
            ToolCallDecision(tool="scan_dependency", done=False, thought="redundant 2"),
            # These should never be reached due to forced convergence:
            ToolCallDecision(tool="scan_code", done=False, thought="unreachable"),
        ]
    )
    monkeypatch.setattr(
        gateway,
        "complete_structured",
        lambda *, prompt, schema, name="": (next(decisions), None),
    )
    monkeypatch.setattr(_loop, "_run_supplement_phase", lambda *a, **k: None)
    monkeypatch.setattr(_loop, "_run_verification_loop", lambda *a, **k: None)
    # After forced convergence, acc.code_report is None → _run_driven falls back
    # to run_pipeline. Mock it to avoid real model calls.
    monkeypatch.setattr(_loop, "run_pipeline", lambda *a, **k: None)

    registry = default_registry()
    calls: list[str] = []
    original_run = registry.run

    def counting_run(tool_name: str, args: Any, ctx: Any) -> Any:
        calls.append(tool_name)
        return original_run(tool_name, args, ctx)

    monkeypatch.setattr(registry, "run", counting_run)

    written: list[AgentPlan] = []
    with ToolContext() as ctx:
        _run_driven(
            request,
            gateway,
            ctx,
            registry,
            plan,
            plan_writer=lambda p: written.append(p),
            repo_is_url=is_repo_url(str(repo_dir)),
            max_turns=10,
        )

    # Only scan_dependency actually ran; the redundant calls were skipped and
    # forced convergence kicked in after 2 consecutive dedup hits.
    assert calls == ["scan_dependency"]
    assert any("forced done" in n for n in plan.notes)
    assert written


def test_react_policy_feeds_history_to_decide(monkeypatch: Any) -> None:
    """Live mode: the model receives conversation history with prior tool results."""

    repo_dir = Path("/tmp") / "s3-history-test"
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
    gateway = ModelGateway(
        ModelConfig(model="live", mode=ModelMode.LIVE, api_key="", base_url="")
    )

    # Track all prompts the model receives.
    received_prompts: list[str] = []
    decisions = iter(
        [
            ToolCallDecision(tool="scan_dependency", done=False, thought="scan dep"),
            ToolCallDecision(tool="scan_code", done=False, thought="scan code"),
            # After scan_code, no tools remain in specs → _decide returns done
            # deterministically without calling LLM, so no third prompt.
        ]
    )

    def fake_complete(*, prompt: str, schema: Any, name: str = "") -> tuple[Any, None]:
        received_prompts.append(prompt)
        return next(decisions), None

    monkeypatch.setattr(gateway, "complete_structured", fake_complete)
    monkeypatch.setattr(_loop, "_run_supplement_phase", lambda *a, **k: None)
    monkeypatch.setattr(_loop, "_run_verification_loop", lambda *a, **k: None)

    with ToolContext() as ctx:
        _run_driven(
            request,
            gateway,
            ctx,
            default_registry(),
            plan,
            plan_writer=lambda p: None,
            repo_is_url=False,
            max_turns=10,
        )

    # Two LLM calls: turn 1 (scan_dep) and turn 2 (scan_code).
    # Turn 3 never calls LLM because no tools remain in specs.
    assert len(received_prompts) == 2
    # First prompt (turn 1): no history entries yet.
    assert "[Turn " not in received_prompts[0]
    # Second prompt (turn 2): should contain the scan_dependency result from turn 1.
    assert "[Turn 1] scan_dependency" in received_prompts[1]
    assert "source version:" in received_prompts[1]
