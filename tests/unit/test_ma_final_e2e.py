"""Final end-to-end wiring tests: Router -> Supervisor -> parallel fan-out ->
aggregate -> evidence gate -> checkpoint recovery, plus MA-4 template unification.

All fake-mode, offline-reproducible; this is the demo scenario of the
implementation plan's acceptance section.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.agent.checkpoint import MemoryCheckpointStore
from upgradelens.agent.decomposer import DynamicDecomposer
from upgradelens.agent.execution_plan import leaf_subplan
from upgradelens.agent.router import Router
from upgradelens.agent.runtime import (
    AgentIdentity,
    AgentKind,
    AgentRunContext,
    TaskEnvelope,
)
from upgradelens.agent.spec import default_registry
from upgradelens.agent.supervisor import AgentContext, decompose_task, run_supervisor
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

_REPO = str(
    (
        Path(__file__).resolve().parent.parent.parent
        / "tests"
        / "fixtures"
        / "eval"
        / "capabilities"
        / "repo"
    ).as_posix()
)
_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@\n"
    "-    a = 1\n"
    "+    a = 2\n"
)


def _multi_task() -> SoftwareTask:
    goal = "review 这个 PR https://github.com/x/y 并做安全扫描"
    context = TaskContext(repo=_REPO, unified_diff=_DIFF)
    return SoftwareTask(
        task_id="t-e2e", kind=TaskKind.PR_REVIEW, goal=goal, context=context
    )


def test_e2e_router_to_evidence_gate():
    """Router classifies 2 capabilities; supervisor runs them in parallel and
    the shared evidence gate converges them (fake mode, fully offline)."""
    task = _multi_task()
    kinds = decompose_task(task, AgentContext(mode="fake"))
    assert {k.value for k in kinds} == {"pr_review", "security_review"}
    # the free-text front door routes the same request to a review kind
    intent = Router().route(task.goal)
    assert intent.task_kind in (TaskKind.PR_REVIEW, TaskKind.SECURITY_REVIEW)

    sup = run_supervisor(task, AgentContext(mode="fake"), mode="fake")
    assert sup.orchestration == "multi-agent"
    # every verified finding in the aggregate carries evidence (MA-3 gate)
    assert sup.aggregate_result is not None
    for f in sup.aggregate_result["findings"]:
        if f["status"] == "verified":
            assert f["evidence_ids"]
    # the run tree: 2 leaves as children of the supervisor root + 1 fan-in
    leaves = [r for r in sup.agent_runs if r["kind"] != "evidence_reviewer"]
    assert len(leaves) == 2
    assert all(r["parent_run_id"] == sup.root_run_id for r in leaves)


def test_e2e_checkpoint_resume_skips_completed_leaves():
    """Crash recovery: re-dispatching the same run id resumes both leaves."""
    task = _multi_task()
    store = MemoryCheckpointStore()
    run_id = "e2e-resume-run"

    first = run_supervisor(
        task, AgentContext(mode="fake"), mode="fake", run_id=run_id, checkpoint_store=store
    )
    assert first.resumed_steps == []
    # both leaves checkpointed under (run_id, step_id)
    for sid in ("cap-0-security_review", "cap-1-pr_review"):
        cp = store.load(run_id, sid)
        assert cp is not None and cp.state["status"] == "completed"

    second = run_supervisor(
        task, AgentContext(mode="fake"), mode="fake", run_id=run_id, checkpoint_store=store
    )
    # same run id -> both completed leaves restored, nothing re-executed
    assert sorted(second.resumed_steps) == [
        "cap-0-security_review",
        "cap-1-pr_review",
    ]
    assert second.root_run_id == run_id
    # the aggregate is still rebuilt and evidence-gated from restored results
    assert second.aggregate_result is not None
    assert second.aggregate_result["kind"] == "evidence_reviewer"

    # a fresh run id shares nothing with the checkpointed one
    third = run_supervisor(task, AgentContext(mode="fake"), mode="fake", checkpoint_store=store)
    assert third.resumed_steps == []


def test_e2e_supervisor_and_decomposer_share_the_template():
    """MA-4 unification: the decomposer's DAG and the supervisor's DAG are the
    same canonical fan-out/fan-in template (same sink, same edge semantics)."""
    # supervisor side
    sup = run_supervisor(_multi_task(), AgentContext(mode="fake"), mode="fake")
    sup_steps = sup.execution_plan["steps"]

    # decomposer side (meta goal full_audit)
    registry = default_registry()
    decomposer = DynamicDecomposer(registry)
    meta = TaskEnvelope(kind="full_audit", goal="full audit", repo=_REPO)
    plan = decomposer.decompose(meta)
    dec_steps = plan.model_dump(mode="json")["steps"]

    # both have exactly one evidence-review fan-in sink with the same id
    sinks = [s for s in sup_steps.values() if s["strategy"] == "fan_in"]
    dec_sinks = [s for s in dec_steps.values() if s["strategy"] == "fan_in"]
    assert len(sinks) == 1 and len(dec_sinks) == 1
    assert sinks[0]["kind"] == dec_sinks[0]["kind"] == "evidence_reviewer"
    # all other steps are parallel leaves
    assert all(
        s["strategy"] == "parallel"
        for s in sup_steps.values()
        if s["strategy"] != "fan_in"
    )
    assert all(
        s["strategy"] == "parallel"
        for s in dec_steps.values()
        if s["strategy"] != "fan_in"
    )
    # the executable leaf sub-plan contains no sink
    assert "evidence_review" not in leaf_subplan(plan).steps


def test_e2e_decompose_and_run_produces_gated_aggregate():
    """The decomposer path runs end-to-end through the unified runtime."""
    registry = default_registry()
    decomposer = DynamicDecomposer(registry)
    ctx = AgentRunContext(
        run_id="e2e-decomp",
        agent=AgentIdentity.create(AgentKind.PR_REVIEW),
        mode="fake",
        locale="zh-CN",
    )
    meta = TaskEnvelope(kind="full_audit", goal="full audit", repo=_REPO)
    reviewed, plan = decomposer.decompose_and_run(meta, ctx, max_workers=2)
    assert reviewed.kind == "evidence_reviewer"
    assert "evidence-gated" in reviewed.summary
    # full_audit fans out to PR + security (+ dependency upgrade not triggered:
    # no has_dependencies flag), converging on the sink
    leaf_ids = [sid for sid in plan.steps if sid != "evidence_review"]
    assert sorted(leaf_ids) == ["leaf_pr_review", "leaf_security_review"]
