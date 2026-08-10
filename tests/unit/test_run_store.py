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

from upgradelens.agent.run_store import RunStore, redact_text
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
    a = RunStore.create(base, "run-stable")
    b = RunStore.create(base, "run-stable")
    intent = _intent()
    a.write_plan(mode="fake", intent=intent)
    b.write_plan(mode="fake", intent=intent)
    assert (a.run_dir / "plan.json").read_text() == (b.run_dir / "plan.json").read_text()

    plan = json.loads((a.run_dir / "plan.json").read_text())
    assert plan["mode"] == "fake"
    assert plan["kind"] == "upgrade_task"
    assert plan["request"]["dependency"] == "pydantic"
    assert [step["order"] for step in plan["steps"]] == [1, 2, 3, 4, 5]
    assert all("tool" in step and "purpose" in step for step in plan["steps"])


def test_trace_jsonl_is_one_event_per_line_with_required_fields(tmp_path: object) -> None:
    store = RunStore.create(Path(str(tmp_path)), "run-trace")
    trace = ToolTrace()
    trace.record(
        tool="scan_code", target="mod.py", params={"repo": "owner/repo"}, status="ok", latency_ms=12
    )
    trace.record(
        tool="retrieve_docs", target="pydantic", params={"top_k": 5}, status="ok", latency_ms=7
    )
    store.write_trace(trace)

    lines = (store.run_dir / "trace.jsonl").read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        event = json.loads(line)
        assert {"tool", "status", "latency_ms", "timestamp"} <= set(event)
        assert event["tool"] in {"scan_code", "retrieve_docs"}


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
