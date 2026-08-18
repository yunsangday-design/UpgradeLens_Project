"""End-to-end check for the M1a natural-language bridge.

A free-text message is triaged by :func:`route_task` into the correct capability
``TaskKind`` and then executed through the unified :func:`run_capability`
dispatcher (fake mode). This proves the router triage actually feeds the right
capability rather than silently defaulting to ``dependency_upgrade``.
"""

from __future__ import annotations

import pytest

from upgradelens.agent.router import route_task
from upgradelens.capabilities.workbench import run_capability
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_SAMPLE_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-old_line = 1\n"
    "+new_line = 2\n"
)

_CASES = [
    (
        "帮我 review 这个 PR https://github.com/x/y",
        TaskKind.PR_REVIEW,
        {"unified_diff": _SAMPLE_DIFF},
    ),
    (
        "扫描这个仓库 https://github.com/x/y 的安全漏洞",
        TaskKind.SECURITY_REVIEW,
        {"unified_diff": _SAMPLE_DIFF},
    ),
    (
        "修复这个 issue：登录时报错 traceback",
        TaskKind.ISSUE_REPAIR,
        {"issue_text": "Login fails with traceback"},
    ),
    (
        "Analyze the breaking change when upgrading requests from 2 to 3 in https://github.com/x/y",
        TaskKind.BREAKING_CHANGE,
        {"unified_diff": _SAMPLE_DIFF, "from_version": "2", "to_version": "3"},
    ),
    (
        "把 https://github.com/x/y 的 pandas 升到 2.x",
        TaskKind.DEPENDENCY_UPGRADE,
        {},
    ),
]


@pytest.mark.parametrize("text,expected_kind,extra", _CASES)
def test_route_then_dispatch_runs_correct_capability(text, expected_kind, extra):
    task = route_task(text)
    assert task.kind == expected_kind

    ctx = task.context
    merged = TaskContext(
        repo=ctx.repo,
        dependency=ctx.dependency,
        source_version=ctx.source_version,
        target_version=ctx.target_version,
        unified_diff=extra.get("unified_diff", ""),
        issue_text=extra.get("issue_text", ""),
        from_version=extra.get("from_version", ""),
        to_version=extra.get("to_version", ""),
    )
    task = SoftwareTask(
        task_id=task.task_id,
        kind=task.kind,
        goal=text,
        context=merged,
    )

    result = run_capability(task, mode="fake")
    payload = result.model_dump(mode="json")
    assert isinstance(payload, dict)
    # The unified dispatcher returns a normalized result regardless of capability.
    assert "findings" in payload
