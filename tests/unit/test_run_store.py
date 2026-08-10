"""Tests for the Step-2 run artifact store.

Two acceptance checks from ROADMAP Step 2 drive these tests:

1. ``plan.json`` / ``trace.jsonl`` are structurally stable so the same
   natural-language input produces a diffable result.
2. No secret (API key / token) is ever written to disk -- an assertion test
   that fails loudly if a credential ever leaks into an artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upgradelens.agent.planner import build_agent_plan
from upgradelens.agent.run_store import RunStore, redact_text
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.tools.registry import default_registry
from upgradelens.tools.trace import ToolTrace
from upgradelens.verify.models import Conclusion, VerifiedReport


def _intent() -> dict:
    return {
        "kind": "upgrade_task",
        "repo": "https://github.com/owner/repo",
        "dependency": "pydantic",
        "target_version": "2.0",
        "source_version": None,
        "missing": [],
        "confidence": 0.9,
        "clarification": None,
    }


def _report() -> VerifiedReport:
    return VerifiedReport(
        target_dependency="pydantic",
        source_version_spec="1.10",
        target_version_spec="2.0",
        conclusion=Conclusion.NO_IMPACT,
        notes="routine upgrade; no breaking API surface detected",
    )


def test_plan_is_structurally_stable(tmp_path: object) -> None:
    base = Path(str(tmp_path))
    intent = _intent()
    gateway = ModelGateway(ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""))
    plan = build_agent_plan(
        gateway=gateway,
        registry=default_registry(),
        repo=intent["repo"],
        dependency=intent["dependency"],
        target_version=intent["target_version"],
        source_version=intent.get("source_version"),
        repo_is_url=True,
    )
    a = RunStore.create(base, "run-stable")
    b = RunStore.create(base, "run-stable")
    a.write_plan(intent=intent, plan=plan)
    b.write_plan(intent=intent, plan=plan)
    assert (a.run_dir / "plan.json").read_text() == (b.run_dir / "plan.json").read_text()

    data = json.loads((a.run_dir / "plan.json").read_text())
    assert data["mode"] == "fake"
    assert data["kind"] == "upgrade_task"
    assert data["request"]["dependency"] == "pydantic"
    assert [s["tool"] for s in data["steps"]] == [
        "clone_repo",
        "scan_dependency",
        "scan_code",
        "retrieve_for_package",
    ]
    for step in data["steps"]:
        assert {"id", "tool", "seq", "status", "phase", "reason"} <= set(step)


def test_trace_jsonl_is_one_event_per_line_with_required_fields(tmp_path: object) -> None:
    store = RunStore.create(Path(str(tmp_path)), "run-trace")
    trace = ToolTrace()
    trace.record(
        tool="scan_code", target="mod.py", params={"repo": "owner/repo"}, status="ok", latency_ms=12
    )
    trace.record(
        tool="retrieve_for_package",
        target="pydantic",
        params={"top_k": 5},
        status="ok",
        latency_ms=7,
    )
    store.write_trace(trace)

    lines = (store.run_dir / "trace.jsonl").read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        event = json.loads(line)
        assert {"tool", "status", "latency_ms", "timestamp"} <= set(event)
        assert event["tool"] in {"scan_code", "retrieve_for_package"}


def test_no_secret_reaches_disk(tmp_path: object) -> None:
    store = RunStore.create(Path(str(tmp_path)), "run-secret")
    secret = "sk-SUPERSECRETEXAMPLE12345"
    intent = _intent()
    intent["clarification"] = f"token found: api_key={secret}"
    store.write_intent(intent)
    store.write_plan(mode="fake", intent=intent)

    trace = ToolTrace()
    trace.record(
        tool="http_get",
        target="https://example.com",
        params={"token": secret},
        status="ok",
        latency_ms=1,
    )
    store.write_trace(trace)

    report = _report()
    report = report.model_copy(update={"notes": f"fetched with {secret}"})
    store.write_report(report)
    store.write_run_md(intent=intent, mode="fake", verified=report, degradations=())

    written = (
        "intent.json",
        "plan.json",
        "trace.jsonl",
        "report.json",
        "report.md",
        "RUN.md",
    )
    contents = [(store.run_dir / name).read_text() for name in written]
    # Critical: the secret must never reach disk.
    for name, text in zip(written, contents, strict=False):
        assert secret not in text, f"secret leaked into {name}"
    # Redaction actually fired on the files that carried the secret.
    intent_text = (store.run_dir / "intent.json").read_text()
    assert "***" in intent_text
    assert "***" in (store.run_dir / "trace.jsonl").read_text()
    assert "***" in (store.run_dir / "report.json").read_text()


def test_innocent_values_are_not_redacted(tmp_path: object) -> None:
    store = RunStore.create(Path(str(tmp_path)), "run-clean")
    intent = _intent()
    store.write_intent(intent)
    text = (store.run_dir / "intent.json").read_text()
    assert "https://github.com/owner/repo" in text
    assert "pydantic" in text
    assert redact_text("repo url https://github.com/owner/repo is fine") == (
        "repo url https://github.com/owner/repo is fine"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
