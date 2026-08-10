"""Tests for the Step-3 agent planner and ReAct loop.

Offline by construction: the deterministic-fallback path (``run_pipeline`` under
``MODEL_MODE=fake``) is exercised against a local temp repo, and the loop's
argument-injection / decision schema are unit-tested without any network call.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from upgradelens.agent.loop import ToolCallDecision, _Accumulator, _build_args, run_agent
from upgradelens.agent.planner import build_agent_plan
from upgradelens.agent.run_store import DEFAULT_PLAN_STEPS
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.pipeline import AssessmentOutcome, AssessmentRequest
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
    assert [step["tool"] for step in plan["steps"]] == [s["tool"] for s in DEFAULT_PLAN_STEPS]


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

    docs = _build_args("retrieve_docs", {"query": "q"}, acc, req)
    assert docs["db"] == "store.db" and docs["source_id"] == "py" and docs["query"] == "q"

    # No doc store configured -> the loop must refuse retrieve_docs.
    req_no_store = AssessmentRequest(repo="x", dependency="pydantic", target_version="2.0")
    try:
        _build_args("retrieve_docs", {}, acc, req_no_store)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    # scan_code before clone is rejected.
    try:
        _build_args("scan_code", {}, _Accumulator(), req)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_run_agent_fake_delegates_to_pipeline() -> None:
    repo_dir = Path(tempfile.mkdtemp())
    (repo_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["pydantic>=1.10"]\n'
    )
    gateway = _fake_gateway()
    request = AssessmentRequest(repo=str(repo_dir), dependency="pydantic", target_version="2.0")
    ctx = ToolContext()
    result = run_agent(request, gateway, ctx, registry=default_registry())
    assert isinstance(result, AssessmentOutcome)
    assert isinstance(result.verified, VerifiedReport)
    # Fake pipeline should have recorded tool calls in the trace.
    assert ctx.trace.to_dict()


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
