"""Tests for the AgentSkill behaviour eval (SK-1-4)."""

from __future__ import annotations

from upgradelens.agent_skills.resolver import default_agent_skill_registry
from upgradelens.eval.agent_skill_eval import (
    GOLD_CASES,
    MAX_NEGATIVE_TRIGGER,
    MIN_PRECISION,
    MIN_RECALL,
    MIN_TOKEN_SAVINGS,
    SkillGoldCase,
    run_agent_skill_eval,
)


def test_eval_passes_plan_thresholds() -> None:
    report = run_agent_skill_eval()
    assert report.passed(), report.failures
    assert report.precision >= MIN_PRECISION
    assert report.recall >= MIN_RECALL
    assert report.f1 > 0.0
    assert report.negative_trigger_rate <= MAX_NEGATIVE_TRIGGER
    # progressive disclosure: no full body injected
    assert report.body_injection_rate == 0.0
    # 17.2: a below-threshold token saving degrades to a warning, not a red X
    # (the built-in bodies are deliberately terse), and never passes silently.
    if report.token_savings < MIN_TOKEN_SAVINGS:
        assert report.threshold_warnings, "below-threshold savings must warn"
    assert 0.0 < report.token_savings < 1.0


def test_eval_flags_wrong_routing_and_negative_triggers() -> None:
    # a gold case that contradicts the routing contract must FAIL the eval,
    # not silently pass -- the gold set is the contract's executable spec.
    bad = (SkillGoldCase("issue_repair", "evidence-grounded-review", "wrong label"),)
    report = run_agent_skill_eval(bad)
    assert not report.passed()
    assert any("issue_repair" in f for f in report.failures)
    assert report.false_positives == 1

    negative_triggered = (SkillGoldCase("pr_review", None, "must not trigger"),)
    neg_report = run_agent_skill_eval(negative_triggered)
    assert not neg_report.passed()
    assert any("must not trigger" in f for f in neg_report.failures)


def test_gold_set_matches_routing_contract() -> None:
    # every positive gold case agrees with the resolver's routing table
    registry = default_agent_skill_registry()
    for case in GOLD_CASES:
        resolved = registry.resolve(case.kind, locale="zh-CN")
        got = resolved.skill_id if resolved is not None else None
        assert got == case.expected, f"{case.kind}: {got} != {case.expected}"


def test_live_only_metrics_are_honest_nones() -> None:
    # fake/replay mode cannot measure instruction-following or task-success
    # delta; the eval must report None instead of inventing a number.
    report = run_agent_skill_eval()
    assert report.instruction_follow_rate is None
    assert report.task_success_delta is None


def test_token_savings_is_computed_from_real_bodies() -> None:
    report = run_agent_skill_eval()
    # candidate-scan disclosure: selected skill contributes instructions,
    # the other candidates only L1 metadata -> strictly less than all bodies
    assert report.token_savings_by_kind
    for kind, savings in report.token_savings_by_kind.items():
        assert 0.0 < savings < 1.0, f"{kind}: {savings}"
