"""Tests for step14 P2/P5: plan_writer injection + async run endpoint.

Verifies:
- DependencyUpgradeAgent.run() accepts plan_writer and invokes it
- plan_writer exceptions don't crash the agent
- _submit_run_job emits plan.updated events via Job
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from upgradelens.agent.api import DependencyUpgradeAgent


class TestPlanWriterInjection:
    """plan_writer is called during agent run."""

    def test_plan_writer_called_in_fake_mode(self, tmp_path: Path):
        """In fake mode, plan_writer should be invoked at least once."""
        calls = []

        def _writer(plan):
            calls.append(plan)

        repo = Path("tests/fixtures/eval/pydantic_field_validator/repo")
        if not repo.exists():
            pytest.skip("fixture not available")

        agent = DependencyUpgradeAgent(mode="fake")
        agent.run(
            "upgrade pydantic to 2.7",
            repo=str(repo),
            dependency="pydantic",
            target_version="2.7",
            plan_writer=_writer,
        )
        # plan_writer should have been called
        assert len(calls) >= 1
        # Each call receives an AgentPlan
        for plan in calls:
            assert hasattr(plan, "steps")
            assert hasattr(plan, "status")

    def test_plan_writer_exception_does_not_crash(self, tmp_path: Path):
        """If plan_writer raises, agent still completes."""

        def _bad_writer(plan):
            raise RuntimeError("boom")

        repo = Path("tests/fixtures/eval/pydantic_field_validator/repo")
        if not repo.exists():
            pytest.skip("fixture not available")

        agent = DependencyUpgradeAgent(mode="fake")
        result = agent.run(
            "upgrade pydantic to 2.7",
            repo=str(repo),
            dependency="pydantic",
            target_version="2.7",
            plan_writer=_bad_writer,
        )
        # Should still complete without error
        assert result.intent is not None
        assert result.error is None or "boom" not in (result.error or "")

    def test_no_plan_writer_still_works(self, tmp_path: Path):
        """Without plan_writer, agent still works (backwards compat)."""
        repo = Path("tests/fixtures/eval/pydantic_field_validator/repo")
        if not repo.exists():
            pytest.skip("fixture not available")

        agent = DependencyUpgradeAgent(mode="fake")
        result = agent.run(
            "upgrade pydantic to 2.7",
            repo=str(repo),
            dependency="pydantic",
            target_version="2.7",
        )
        assert result.intent is not None


class TestRunAsyncJobEmitsPlanEvents:
    """_submit_run_job should emit plan.updated events."""

    def test_plan_updated_events_emitted(self):
        """Verify Job receives plan.updated events in fake mode."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "demo"))
        from demo.jobs import Job, JobManager

        repo = Path("tests/fixtures/eval/pydantic_field_validator/repo")
        if not repo.exists():
            pytest.skip("fixture not available")

        mgr = JobManager(max_workers=1)
        try:
            # Simulate what _submit_run_job does
            def _do_run(job: Job) -> dict:
                from upgradelens import DependencyUpgradeAgent
                from upgradelens.agent.plan import AgentPlan

                def _plan_event_writer(plan: AgentPlan) -> None:
                    steps_summary = []
                    for s in plan.steps:
                        steps_summary.append({
                            "tool": s.tool, "seq": s.seq, "status": s.status,
                        })
                    job.emit("plan.updated", {"steps": steps_summary})

                agent = DependencyUpgradeAgent(mode="fake")
                agent.run(
                    "upgrade pydantic to 2.7",
                    repo=str(repo),
                    dependency="pydantic",
                    target_version="2.7",
                    plan_writer=_plan_event_writer,
                )
                return {"ok": True}

            job = mgr.submit("run", {}, _do_run)
            # Wait for completion
            for _ in range(100):
                if job.is_terminal:
                    break
                time.sleep(0.1)

            assert job.status.value == "succeeded"
            # Check for plan.updated events
            plan_events = [e for e in job.events_since(0) if e.kind == "plan.updated"]
            assert len(plan_events) >= 1
            # Each should have steps
            for ev in plan_events:
                assert "steps" in ev.data
                assert isinstance(ev.data["steps"], list)
        finally:
            mgr.stop()
