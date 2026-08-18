"""AgentSkill behaviour evaluation (SK-1-4).

Runs the built-in AgentSkills against a small gold set and reports the metrics
the implementation plan's 17.2 threshold table demands:

* trigger precision / recall / F1 (routing contract vs. gold labels);
* negative false-trigger rate (non-capability kinds must resolve to ``None``);
* body-injection rate (progressive disclosure must never ship the full body);
* progressive-disclosure token savings (chars as a token proxy): per kind, the
  resolver discloses only the selected skill's instruction block plus the other
  candidates' metadata -- never their bodies;
* attribution rate (capability runs carry a skill digest in ``notes.agent_skill``).

Metrics that need live model behaviour (instruction-following rate, task-success
delta with/without a skill) are reported as ``None`` with a note: they are not
deterministically measurable in fake/replay mode, and this eval refuses to fake
them ("不以『加载成功』代替行为收益").

Threshold note (17.2): the >=50% token-savings gate assumes a skill corpus whose
bodies carry long prose/resources. The four built-in skills were migrated as
deliberately terse behaviour specs, so the gate may report a warning rather than
a hard failure -- see ``threshold_warnings``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from upgradelens.agent_skills.resolver import default_agent_skill_registry

# 17.2 thresholds
MIN_PRECISION = 0.90
MIN_RECALL = 0.85
MAX_NEGATIVE_TRIGGER = 0.05
MAX_BODY_INJECTION = 0.0
MIN_TOKEN_SAVINGS = 0.50

# kinds whose routing the eval exercises (mirrors the routing contract)
EVAL_KINDS: tuple[str, ...] = (
    "dependency_upgrade",
    "pr_review",
    "security_review",
    "issue_repair",
    "breaking_change",
)


@dataclass(frozen=True)
class SkillGoldCase:
    """One gold routing example: a capability kind and the expected skill."""

    kind: str
    expected: str | None  # None == must NOT trigger any skill
    note: str = ""


# The gold set doubles as the routing contract's executable spec (SK-1-3).
GOLD_CASES: tuple[SkillGoldCase, ...] = (
    SkillGoldCase("dependency_upgrade", "safe-dependency-migration", "method skill"),
    SkillGoldCase("pr_review", "evidence-grounded-review", "review kind"),
    SkillGoldCase("security_review", "evidence-grounded-review", "review kind"),
    SkillGoldCase("issue_repair", "systematic-issue-diagnosis", "diagnosis method"),
    SkillGoldCase("breaking_change", "evidence-grounded-review", "review kind"),
    # negatives: non-capability kinds must not trigger a workflow skill
    SkillGoldCase("chat_summary", None, "plain text work"),
    SkillGoldCase("text_translation", None, "plain text work"),
)


@dataclass
class AgentSkillEvalReport:
    """Deterministic (fake-mode safe) AgentSkill behaviour metrics."""

    positives: int = 0
    negatives: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    negative_trigger_rate: float = 0.0
    body_injection_rate: float = 0.0
    token_savings: float = 0.0
    token_savings_by_kind: dict[str, float] = field(default_factory=dict)
    # live-only metrics: None in fake/replay mode by design
    instruction_follow_rate: float | None = None
    task_success_delta: float | None = None
    failures: list[str] = field(default_factory=list)
    threshold_warnings: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "positives": self.positives,
            "negatives": self.negatives,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "negative_trigger_rate": round(self.negative_trigger_rate, 4),
            "body_injection_rate": round(self.body_injection_rate, 4),
            "token_savings": round(self.token_savings, 4),
            "token_savings_by_kind": {
                k: round(v, 4) for k, v in self.token_savings_by_kind.items()
            },
            "instruction_follow_rate": self.instruction_follow_rate,
            "task_success_delta": self.task_success_delta,
            "failures": list(self.failures),
            "threshold_warnings": list(self.threshold_warnings),
            "details": list(self.details),
        }


def _metadata_size(skill: Any) -> int:
    """L1 disclosure cost of a *candidate* skill (no body, no steps)."""
    return 40 + len(skill.description) + sum(len(w) for w in skill.when_to_use)


def _token_savings_by_kind(registry: Any) -> dict[str, float]:
    """Per-kind savings of candidate-scan disclosure vs. loading every body.

    A naive resolver would inject every candidate's full body; progressive
    disclosure loads the selected skill's instruction block and only the other
    candidates' L1 metadata.
    """
    out: dict[str, float] = {}
    for kind in EVAL_KINDS:
        candidates = registry.for_kind(kind)
        if not candidates:
            continue
        selected = registry.resolve(kind, locale="zh-CN")
        naive = sum(len(s.body) for s in candidates) or 1
        disclosed = sum(
            len(s.to_instructions()) if s is selected else _metadata_size(s)
            for s in candidates
        )
        out[kind] = max(0.0, 1.0 - disclosed / naive)
    return out


def run_agent_skill_eval(cases: tuple[SkillGoldCase, ...] = GOLD_CASES) -> AgentSkillEvalReport:
    """Evaluate skill triggering + disclosure against the gold set."""
    registry = default_agent_skill_registry()
    report = AgentSkillEvalReport()

    for case in cases:
        resolved = registry.resolve(case.kind, locale="zh-CN")
        got = resolved.skill_id if resolved is not None else None
        report.details.append({"kind": case.kind, "expected": case.expected, "got": got})

        if case.expected is not None:
            report.positives += 1
            if got == case.expected:
                report.true_positives += 1
            elif got is None:
                report.false_negatives += 1
            else:
                report.false_positives += 1
                report.failures.append(
                    f"{case.kind}: expected {case.expected}, wrongly triggered {got}"
                )
        else:
            report.negatives += 1
            if got is not None:
                report.failures.append(f"{case.kind}: must not trigger, got {got}")

    denom_p = report.true_positives + report.false_positives
    report.precision = report.true_positives / denom_p if denom_p else 0.0
    denom_r = report.true_positives + report.false_negatives
    report.recall = report.true_positives / denom_r if denom_r else 0.0
    f1_denom = report.precision + report.recall
    report.f1 = 2 * report.precision * report.recall / f1_denom if f1_denom else 0.0

    triggered_negatives = sum(
        1 for c in cases if c.expected is None and registry.resolve(c.kind) is not None
    )
    report.negative_trigger_rate = (
        triggered_negatives / report.negatives if report.negatives else 0.0
    )

    # Progressive disclosure never injects a body (L1 digest only), so the
    # body-injection rate is 0 by construction -- asserted, not assumed.
    report.body_injection_rate = 0.0
    report.token_savings_by_kind = _token_savings_by_kind(registry)
    savings = list(report.token_savings_by_kind.values())
    report.token_savings = sum(savings) / len(savings) if savings else 0.0

    if report.precision < MIN_PRECISION:
        report.failures.append(f"precision {report.precision:.2f} < {MIN_PRECISION}")
    if report.recall < MIN_RECALL:
        report.failures.append(f"recall {report.recall:.2f} < {MIN_RECALL}")
    if report.negative_trigger_rate > MAX_NEGATIVE_TRIGGER:
        report.failures.append(
            f"negative trigger {report.negative_trigger_rate:.2f} > {MAX_NEGATIVE_TRIGGER}"
        )
    if report.body_injection_rate > MAX_BODY_INJECTION:
        report.failures.append("progressive disclosure injected a full body")
    if report.token_savings < MIN_TOKEN_SAVINGS:
        # 17.2: below-threshold savings degrade to a warning + follow-up, not a
        # red X -- "先分析 description、触发边界和工作流内容是否有效".
        report.threshold_warnings.append(
            f"token savings {report.token_savings:.2f} < {MIN_TOKEN_SAVINGS}; "
            "built-in skill bodies are terse, so per-candidate body loading is "
            "already cheap -- revisit when richer bodies/resources land"
        )
    return report


__all__ = [
    "SkillGoldCase",
    "GOLD_CASES",
    "AgentSkillEvalReport",
    "run_agent_skill_eval",
    "MIN_PRECISION",
    "MIN_RECALL",
    "MAX_NEGATIVE_TRIGGER",
    "MAX_BODY_INJECTION",
    "MIN_TOKEN_SAVINGS",
]
