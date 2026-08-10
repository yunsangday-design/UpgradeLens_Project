"""Tests for the Step-3 agent planner and ReAct loop.

Offline by construction: the deterministic-fallback path (``run_pipeline`` under
``MODEL_MODE=fake``) is exercised against a local temp repo, and the loop's
argument-injection / decision schema are unit-tested without any network call.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from upgradelens.agent.loop import ToolCallDecision, _Accumulator, _build_args, run_agent
from upgradelens.agent.plan import AgentPlan
from upgradelens.agent.planner import build_agent_plan
from upgradelens.agent.run_store import DEFAULT_PLAN_STEPS
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.pipeline import AssessmentOutcome, AssessmentRequest
from upgradelens.tools.live_repo import is_repo_url
from upgradelens.tools.registry import ToolContext, default_registry
from upgradelens.verify.models import VerifiedReport


def _fake_gateway() -> ModelGateway:
    return ModelGateway(ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""))


def test_build_agent_plan_fake_returns_default() -> None:
    gateway = _fake_gateway()
    plan = build_agent_plan(
        gateway=gateway,
        registry=default_registry(),
        repo="https://github.com/o/r",
        dependency="pydantic",
        target_version="2.0",
    )
    assert isinstance(plan, AgentPlan)
    assert [step.tool for step in plan.steps] == [s["tool"] for s in DEFAULT_PLAN_STEPS]


def test_tool_call_decision_defaults() -> None:
    decision = ToolCallDecision()
    assert decision.tool is None
    assert decision.done is False
    assert decision.arguments == {}
    assert decision.thought == ""


def test_build_args_injects_known_values() -> None:
    req = AssessmentRequest(
        repo="https://github.com/o/r",
        dependency="pydantic",
        target_version="2.0",
        db="store.db",
        source_id="py",
        ref=None,
    )
    acc = _Accumulator()
    assert _build_args("clone_repo", {}, acc, req) == {"url": "https://github.com/o/r", "ref": None}

    acc.repo_path = Path("/tmp/x")
    scan = _build_args("scan_code", {}, acc, req)
    assert scan["repo"] == "/tmp/x" and scan["dependency"] == "pydantic"

    docs = _build_args("retrieve_for_package", {}, acc, req)
    assert docs["db"] == "store.db"
    assert docs["package"] == "pydantic"
    assert docs["source_version"] == ""
    assert docs["target_version"] == ""
    assert docs["source_id"] == "py"
    assert docs["top_k"] == 5

    # No doc store configured -> the loop must refuse retrieve_for_package.
    req_no_store = AssessmentRequest(repo="x", dependency="pydantic", target_version="2.0")
    try:
        _build_args("retrieve_for_package", {}, acc, req_no_store)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    # scan_code before clone is rejected.
    try:
        _build_args("scan_code", {}, _Accumulator(), req)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_run_agent_fake_drives_plan() -> None:
    """Fake mode now drives the same plan-backed state machine (no run_pipeline shortcut)."""
    repo_dir = Path(tempfile.mkdtemp())
    (repo_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["pydantic>=1.10"]\n'
    )
    gateway = _fake_gateway()
    request = AssessmentRequest(repo=str(repo_dir), dependency="pydantic", target_version="2.0")
    plan = build_agent_plan(
        gateway=gateway,
        registry=default_registry(),
        repo=str(repo_dir),
        dependency="pydantic",
        target_version="2.0",
        repo_is_url=is_repo_url(str(repo_dir)),
    )
    written: list[AgentPlan] = []
    ctx = ToolContext()
    result = run_agent(
        request, gateway, ctx, registry=default_registry(), plan=plan,
        plan_writer=lambda p: written.append(p),
    )
    # The driven loop produced a verified outcome and recorded trace events.
    assert isinstance(result, AssessmentOutcome)
    assert isinstance(result.verified, VerifiedReport)
    assert ctx.trace.events

    # clone_repo was dropped for a local path; the remaining steps are resolved.
    assert [s.tool for s in plan.steps] == [
        "scan_dependency",
        "scan_code",
        "retrieve_for_package",
        "supplement_retrieval",
    ]
    assert plan.is_resolved()
    assert plan.steps[0].status == "succeeded"  # scan_dependency
    assert plan.steps[1].status == "succeeded"  # scan_code
    # No doc store -> retrieve_for_package is recorded as skipped, not failed.
    assert plan.steps[2].status == "skipped"
    # The plan was written back at least once during execution.
    assert written
    # Trace events carry the owning plan step id.
    assert all(e.plan_step_id for e in ctx.trace.events)


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
