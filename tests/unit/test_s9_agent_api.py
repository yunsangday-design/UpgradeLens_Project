"""Tests for the DependencyUpgradeAgent unified API (S9)."""

from __future__ import annotations

from pathlib import Path

from upgradelens import AgentResult, DependencyUpgradeAgent

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"
ALIAS_REPO = CASES_DIR / "alias_import" / "repo"


def test_agent_runs_upgrade_task():
    """A valid upgrade goal with explicit repo/dependency produces an assessment."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic to 2.0",
        repo=ALIAS_REPO,
        dependency="pydantic",
        target_version="2.0",
    )
    assert isinstance(result, AgentResult)
    assert result.intent.kind == "upgrade_task"
    assert result.intent.dependency == "pydantic"
    assert result.outcome is not None
    assert result.verified is not None
    assert result.plan is not None


def test_agent_non_upgrade_intent_returns_early():
    """A non-upgrade request does not trigger the pipeline."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run("hello world")
    assert result.intent.kind == "not_upgrade"
    assert result.outcome is None
    assert result.verified is None


def test_agent_dry_run_does_not_assess():
    """Dry-run produces a plan but no assessment."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic to 2.0",
        repo=ALIAS_REPO,
        dependency="pydantic",
        target_version="2.0",
        dry_run=True,
    )
    assert result.intent.kind == "upgrade_task"
    assert result.plan is not None
    assert result.outcome is None


def test_agent_writes_artifacts(tmp_path):
    """When out_dir is given, artifacts are written."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic to 2.0",
        repo=ALIAS_REPO,
        dependency="pydantic",
        target_version="2.0",
        out_dir=tmp_path,
    )
    assert result.run_dir is not None
    run_dir = Path(result.run_dir)
    assert (run_dir / "intent.json").exists()
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "report.json").exists()
    assert (run_dir / "assessment.json").exists()
    assert (run_dir / "upgrade-plan.json").exists()
    assert (run_dir / "upgrade-plan.md").exists()
    assert (run_dir / "RUN.md").exists()


def test_agent_explicit_kwargs_promote_need_clarification():
    """Explicit repo+dependency+target promotes need_clarification to upgrade_task."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "看看这个仓库",
        repo=ALIAS_REPO,
        dependency="pydantic",
        target_version="2.0",
    )
    assert result.intent.kind == "upgrade_task"
    assert result.outcome is not None


def test_agent_run_pipeline_directly():
    """run_pipeline runs the deterministic pipeline without the agent loop."""
    agent = DependencyUpgradeAgent(mode="fake")
    outcome = agent.run_pipeline(
        repo=str(ALIAS_REPO),
        dependency="pydantic",
        target_version="2.0",
    )
    assert outcome.verified is not None


def test_agent_gateway_has_ledger():
    """The result's gateway has a non-empty ledger after an agent run."""
    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(
        "upgrade pydantic to 2.0",
        repo=ALIAS_REPO,
        dependency="pydantic",
        target_version="2.0",
    )
    assert result.gateway is not None
    assert len(result.gateway.ledger) > 0
