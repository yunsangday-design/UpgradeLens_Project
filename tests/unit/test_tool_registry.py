"""Tests for the uniform Tool abstraction.

The registry is the seam a future agent loop will call through, so the tests
focus on the guarantees that seam must provide: schemas are well-formed, bad
arguments are rejected before any work happens, results are JSON-safe, failures
arrive as ``ToolError`` subclasses, and every call lands in the trace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from upgradelens.tools.errors import ToolError, ToolExecutionError, ToolInputError
from upgradelens.tools.registry import (
    BUILTIN_TOOLS,
    Tool,
    ToolContext,
    ToolRegistry,
    default_registry,
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "model.py").write_text(
        "import pydantic\n\nclass User(pydantic.BaseModel):\n    name: str\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text("pydantic==1.10.2\n", encoding="utf-8")
    return repo


def _assert_jsonable(value: object) -> None:
    json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Registry mechanics
# --------------------------------------------------------------------------- #


def test_default_registry_exposes_the_builtin_tools() -> None:
    registry = default_registry()
    assert registry.names() == [
        "clone_repo",
        "resolve_skill",
        "retrieve_docs",
        "scan_code",
        "scan_dependency",
        "verify_report",
    ]
    assert len(registry) == len(BUILTIN_TOOLS)
    assert "scan_code" in registry


def test_specs_are_function_calling_shaped() -> None:
    for spec in default_registry().specs():
        assert spec["name"]
        assert spec["description"]
        assert spec["parameters"]["type"] == "object"
        _assert_jsonable(spec)


def test_unknown_tool_raises_tool_input_error() -> None:
    with pytest.raises(ToolInputError, match="unknown tool"):
        default_registry().get("nope")


def test_duplicate_registration_is_rejected() -> None:
    registry = default_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.get("scan_code"))


def test_invalid_arguments_are_rejected_before_execution() -> None:
    with pytest.raises(ToolInputError, match="invalid arguments"):
        default_registry().run("scan_code", {"repo": "/tmp"})  # missing 'dependency'


def test_handler_exceptions_are_normalised() -> None:
    class Input(BaseModel):
        value: str

    def boom(args: Input, ctx: ToolContext) -> dict[str, object]:
        raise RuntimeError("kaboom")

    tool = Tool(name="boom", description="always fails", input_model=Input, handler=boom)
    with pytest.raises(ToolExecutionError, match="kaboom") as excinfo:
        tool.run({"value": "x"})
    assert isinstance(excinfo.value, ToolError)


def test_registry_is_iterable_in_name_order() -> None:
    registry = ToolRegistry(list(BUILTIN_TOOLS))
    assert [tool.name for tool in registry] == registry.names()


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #


def test_successful_call_is_traced(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ctx = ToolContext()
    default_registry().run("scan_code", {"repo": str(repo), "dependency": "pydantic"}, ctx)
    events = ctx.trace.events
    assert len(events) == 1
    assert events[0].tool == "scan_code"
    assert events[0].status == "ok"
    assert events[0].target == str(repo)


def test_failed_call_is_traced_with_the_error() -> None:
    class Input(BaseModel):
        repo: str

    def boom(args: Input, ctx: ToolContext) -> dict[str, object]:
        raise RuntimeError("kaboom")

    ctx = ToolContext()
    tool = Tool(name="boom", description="always fails", input_model=Input, handler=boom)
    with pytest.raises(ToolExecutionError):
        tool.run({"repo": "/tmp/x"}, ctx)
    assert ctx.trace.events[0].status == "error"
    assert "kaboom" in (ctx.trace.events[0].error or "")


# --------------------------------------------------------------------------- #
# Built-in tool behaviour
# --------------------------------------------------------------------------- #


def test_scan_code_returns_jsonable_evidence(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = default_registry().run("scan_code", {"repo": str(repo), "dependency": "pydantic"})
    _assert_jsonable(result)
    assert result["dependency_name"] == "pydantic"
    assert result["scanned_files"] >= 1
    assert result["usages"]


def test_scan_dependency_reads_the_manifest(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = default_registry().run(
        "scan_dependency",
        {"repo": str(repo), "dependency": "pydantic", "target_version": "2.7.0"},
    )
    _assert_jsonable(result)
    assert result["dependency_name"]


def test_resolve_skill_matches_a_builtin_pack() -> None:
    result = default_registry().run(
        "resolve_skill", {"dependency": "pydantic", "target_version": "2.7.0"}
    )
    assert result["matched"] is True
    assert result["skill_id"]
    assert result["patterns"]


def test_resolve_skill_reports_a_miss() -> None:
    result = default_registry().run("resolve_skill", {"dependency": "definitely-not-a-package"})
    assert result == {"skill_id": None, "matched": False}


def test_retrieve_docs_top_k_is_bounded() -> None:
    with pytest.raises(ToolInputError):
        default_registry().run(
            "retrieve_docs",
            {"db": "x.db", "source_id": "s", "query": "q", "top_k": 0},
        )


# --------------------------------------------------------------------------- #
# Context lifecycle
# --------------------------------------------------------------------------- #


def test_context_closes_tracked_clones(tmp_path: Path) -> None:
    class _FakeHandle:
        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    handle = _FakeHandle()
    with ToolContext() as ctx:
        ctx.track_clone(handle)  # type: ignore[arg-type]
    assert handle.cleaned is True


def test_context_reuses_one_session_per_database(tmp_path: Path) -> None:
    db = tmp_path / "evidence.db"
    with ToolContext() as ctx:
        first = ctx.session(db)
        second = ctx.session(db)
        assert first is second
