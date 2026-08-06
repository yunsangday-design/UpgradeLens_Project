"""Externalised prompt templates plus the system-level evidence contract.

Prompts used to be string literals inlined in :mod:`upgradelens.graph.nodes`,
which made them impossible to review, diff or version independently of the
graph wiring. They now live here as :class:`PromptTemplate` values that carry
their own ``version`` and a shared ``system`` preamble.

The system preamble is the *hard* part: it states the evidence contract that
every model call must obey (never invent an evidence id, never claim a file or
symbol that is not in the supplied context). The verifier still enforces the
same rules downstream -- the prompt is a first line of defence, not the only
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

PROMPT_VERSION = "v1"

#: The non-negotiable rules injected ahead of every structured model call.
EVIDENCE_CONTRACT = """\
You are UpgradeLens, an assistant that assesses the blast radius of a
dependency upgrade for one specific repository.

Hard rules -- violating any of them makes the answer unusable:
1. Ground every claim in the evidence supplied in the context. Each evidence
   item is prefixed with its id in square brackets, e.g. [code::foo.py:12].
2. Only ever cite evidence ids that appear verbatim in the context. Never
   invent, guess, abbreviate or reformat an id. If no id supports a claim, drop
   the claim instead of citing something close enough.
3. Never assert the existence of a file, symbol, version or API that the
   context does not show. You cannot read the repository or the network; the
   context is the entire world.
4. Prefer omission over speculation. When the evidence is thin, say so in the
   notes and lower the confidence rather than filling the gap.
5. Answer only with the requested structured object. No prose wrapper, no
   markdown fences, no commentary outside the schema."""


@dataclass(frozen=True)
class PromptTemplate:
    """A named, versioned prompt: a system preamble plus a ``$``-substituted body.

    ``string.Template`` is used instead of :meth:`str.format` because rendered
    values routinely contain braces (code snippets, JSON), which ``format``
    would try to interpret.
    """

    name: str
    version: str
    body: str
    system: str = EVIDENCE_CONTRACT

    def render(self, **values: object) -> str:
        """Return the full prompt (system preamble + filled body).

        Raises:
            KeyError: if the template references a placeholder not provided.
        """
        rendered = Template(self.body).substitute(
            {key: str(value) for key, value in values.items()}
        )
        return f"{self.system}\n\n{rendered.strip()}"


PLANNER = PromptTemplate(
    name="planner",
    version=PROMPT_VERSION,
    body="""\
You are planning an upgrade impact analysis.
Target dependency: $dependency
Skill patterns:
$patterns
Collected code evidence:
$code_evidence
Return a plan listing the skill pattern ids to inspect, with one question each.""",
)

BREAKING_CHANGE = PromptTemplate(
    name="breaking_change",
    version=PROMPT_VERSION,
    body="""\
Analyze the potential breaking change for pattern '$pattern_id'.
Question: $question

Context evidence:
$context

Return a BreakingChange. Reference only evidence ids present in the context.""",
)

IMPACT_REPORT = PromptTemplate(
    name="impact_report",
    version=PROMPT_VERSION,
    body="""\
Produce the final upgrade impact report for '$dependency'.
Plan:
$plan
Breaking changes:
$breaking_changes
Context evidence:
$context

Return an ImpactReport. Every risk MUST reference only evidence ids that appear
in the context.""",
)

PROMPTS: dict[str, PromptTemplate] = {
    template.name: template for template in (PLANNER, BREAKING_CHANGE, IMPACT_REPORT)
}


def get_prompt(name: str) -> PromptTemplate:
    """Look up a registered prompt template by name."""
    try:
        return PROMPTS[name]
    except KeyError:
        raise KeyError(f"unknown prompt template: {name!r}") from None


__all__ = [
    "BREAKING_CHANGE",
    "EVIDENCE_CONTRACT",
    "IMPACT_REPORT",
    "PLANNER",
    "PROMPTS",
    "PROMPT_VERSION",
    "PromptTemplate",
    "get_prompt",
]
