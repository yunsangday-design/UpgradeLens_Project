"""ROADMAP Step 1 (A2): intent routing acceptance tests.

The four acceptance criteria from the roadmap are encoded as the first four
tests. They must pass with no model call (the router's rule layer is what makes
the ``fake`` mode fully offline), so they use a bare :class:`Router` with no
gateway. The LLM refinement path is exercised separately with a stub gateway.
"""

from __future__ import annotations

from typing import Any

from upgradelens.agent.router import Intent, Router, route, route_task
from upgradelens.core.task import TaskKind
from upgradelens.llm.gateway import ModelMode


class _StubGateway:
    """Minimal gateway double: non-fake mode, returns a canned Intent.

    Router only touches ``.mode`` and ``.complete_structured``, so duck typing
    is enough to exercise the live path offline.
    """

    mode = ModelMode.LIVE

    def __init__(self, intent: Intent) -> None:
        self._intent = intent

    def complete_structured(self, *, prompt: str, schema: Any, name: str) -> tuple[Any, None]:
        return self._intent, None


def test_upgrade_task_extracts_three_elements():
    intent = Router().route("把 https://github.com/x/y 的 pandas 升到 2.x")
    assert intent.kind == "upgrade_task"
    assert intent.repo == "https://github.com/x/y"
    assert intent.dependency == "pandas"
    assert intent.target_version == "2.x"


def test_not_upgrade_short_circuits():
    intent = Router().route("今天天气怎么样")
    assert intent.kind == "not_upgrade"
    assert intent.repo is None
    assert intent.dependency is None


def test_need_clarification_reports_missing():
    intent = Router().route("帮我看看这个仓库")
    assert intent.kind == "need_clarification"
    assert set(intent.missing) == {"repo", "dependency", "target_version"}
    assert "repository" in (intent.clarification or "")


def test_invalid_url_blocked_before_llm_internal_ip():
    intent = Router().route("把 http://127.0.0.1/foo/bar 升到 2.0")
    assert intent.kind == "invalid_url"
    assert intent.clarification


def test_invalid_url_blocked_non_github_domain():
    intent = Router().route("把 http://evil.com/foo/bar 升到 2.0")
    assert intent.kind == "invalid_url"
    assert intent.clarification


def test_convenience_route_function():
    intent = route("把 https://github.com/a/b 的 pydantic 升到 2.0")
    assert intent.kind == "upgrade_task"
    assert intent.dependency == "pydantic"


def test_llm_refines_missing_dependency():
    # Rule finds repo + target but no dependency; the model fills it in.
    gateway = _StubGateway(
        Intent(kind="upgrade_task", repo="https://github.com/x/y", dependency="pydantic")
    )
    intent = Router(gateway=gateway).route("把 https://github.com/x/y 升到 2.0")
    assert intent.kind == "upgrade_task"
    assert intent.dependency == "pydantic"
    assert intent.repo == "https://github.com/x/y"


def test_llm_invented_invalid_repo_is_discarded():
    # The model returns an internal repo with no mention of one in the text;
    # the validated rule repo (None here) wins, so it stays missing.
    gateway = _StubGateway(
        Intent(kind="upgrade_task", repo="http://127.0.0.1/a/b", dependency="pydantic")
    )
    intent = Router(gateway=gateway).route("把 pydantic 升到 2.0")
    assert intent.repo is None
    assert "repo" in intent.missing
    assert intent.kind == "need_clarification"


# --- M1a: the router must triage free text into the right capability TaskKind ---


def test_route_task_triages_pr_review():
    task = route_task("帮我 review 这个 PR https://github.com/x/y")
    assert task.kind == TaskKind.PR_REVIEW
    assert task.context.repo == "https://github.com/x/y"


def test_route_task_triages_security_review():
    task = route_task("扫描这个仓库 https://github.com/x/y 的安全漏洞")
    assert task.kind == TaskKind.SECURITY_REVIEW


def test_route_task_triages_issue_repair():
    task = route_task("修复这个 issue：登录时报错 traceback")
    assert task.kind == TaskKind.ISSUE_REPAIR


def test_route_task_triages_breaking_change():
    task = route_task(
        "Analyze the breaking change when upgrading requests from 2 to 3 in https://github.com/x/y"
    )
    assert task.kind == TaskKind.BREAKING_CHANGE


def test_route_task_triages_dependency_upgrade_unchanged():
    task = route_task("把 https://github.com/x/y 的 pandas 升到 2.x")
    assert task.kind == TaskKind.DEPENDENCY_UPGRADE


def test_route_task_chit_chat_stays_unknown():
    task = route_task("今天天气怎么样")
    assert task.kind == TaskKind.UNKNOWN


def test_route_task_security_beats_generic_review():
    # "review" alone must not mask an explicit security intent when a repo is given.
    task = route_task("review 这个仓库 https://github.com/x/y 的安全性")
    assert task.kind == TaskKind.SECURITY_REVIEW
