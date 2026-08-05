"""Context builder: turn an evidence bundle into a bounded prompt context.

The context intentionally contains only evidence summaries and short snippets.
It never serializes repository source code, so a model cannot "read" the whole
repository through the prompt. A token budget truncates the list.
"""

from __future__ import annotations

from upgradelens.llm.gateway import TokenBudget, estimate_tokens
from upgradelens.models.impact import EvidenceBundle, Plan

_MAX_DETAIL_CHARS = 400


def build_context(
    bundle: EvidenceBundle,
    plan: Plan | None,
    *,
    budget: TokenBudget | None = None,
    max_context_tokens: int = 2000,
) -> str:
    parts: list[str] = []
    used = 0

    if plan and plan.items:
        plan_block = "Analysis plan:\n" + "\n".join(
            f"- {it.pattern_id}: {it.question}" for it in plan.items
        )
        used += estimate_tokens(plan_block)
        parts.append(plan_block)

    truncated = False
    for item in bundle.items:
        detail = item.detail
        if len(detail) > _MAX_DETAIL_CHARS:
            detail = detail[:_MAX_DETAIL_CHARS] + " …"
        block = f"[{item.evidence_id}] ({item.kind}) {item.summary}\n  {detail}"
        t = estimate_tokens(block)
        if budget is not None and budget.remaining_tokens - t < 0:
            truncated = True
            break
        if used + t > max_context_tokens:
            truncated = True
            break
        used += t
        parts.append(block)

    if truncated:
        parts.append("… (context truncated to fit the token budget)")
    return "\n\n".join(parts)


class ContextBuilder:
    def __init__(self, budget: TokenBudget | None = None) -> None:
        self._budget = budget

    def build(
        self,
        bundle: EvidenceBundle,
        plan: Plan | None,
        *,
        max_context_tokens: int = 2000,
    ) -> str:
        return build_context(
            bundle,
            plan,
            budget=self._budget,
            max_context_tokens=max_context_tokens,
        )
