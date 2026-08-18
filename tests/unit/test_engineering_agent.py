"""Offline (fake-mode) tests for the A3 EngineeringAgent unified front door.

Exercises routing, single/multi-capability decomposition and the normalised
``EngineeringResult`` without any network or API key. Live paths are deferred to
live verification.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens import EngineeringAgent, EngineeringResult
from upgradelens.core.task import TaskKind

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = ROOT / "tests/fixtures/eval/pydantic_field_validator/repo"


def _agent() -> EngineeringAgent:
    return EngineeringAgent(mode="fake")


def test_dry_run_routes_issue_repair():
    res = _agent().run("fix bug: login button fails", dry_run=True)
    assert res.dry_run is True
    assert res.error is None
    assert TaskKind.ISSUE_REPAIR in res.kinds
    assert res.orchestration == "single"


def test_dry_run_routes_pr_review_from_url():
    res = _agent().run("review the PR https://github.com/foo/bar", dry_run=True)
    assert res.dry_run is True
    assert TaskKind.PR_REVIEW in res.kinds


def test_dry_run_multi_capability_fanout():
    res = _agent().run(
        "review this PR and run a security scan",
        repo="https://github.com/foo/bar",
        dry_run=True,
    )
    assert res.orchestration == "multi-agent"
    assert set(res.capabilities) >= {"pr_review", "security_review"}
    assert len(res.kinds) >= 2


def test_run_fake_single_issue_repair_succeeds():
    res = _agent().run("fix bug: login button fails")
    assert isinstance(res, EngineeringResult)
    assert res.error is None
    assert res.result is not None
    # single capability => no supervisor aggregate, result holds the payload
    assert res.supervisor is None


def test_unknown_goal_yields_no_capability():
    res = _agent().run("hello there, nice weather today", dry_run=True)
    assert res.kinds == []
    assert res.error is not None
    assert res.degradations == ["no-capability-matched"]


def test_run_fake_security_review_with_repo_kwarg():
    res = _agent().run(
        "scan this repository for security vulnerabilities",
        repo=str(FIXTURE_REPO),
    )
    assert res.error is None
    assert TaskKind.SECURITY_REVIEW in res.kinds
    assert res.result is not None
    assert res.result.status == "succeeded"
