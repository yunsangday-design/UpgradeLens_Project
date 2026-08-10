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

Templates that emit ``evidence_ids`` additionally carry :class:`FewShotExample`
values. The contract *states* the grounding rule; an example *shows* it, which
is what actually moves compliance on the failure mode we care about.
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
class FewShotExample:
    """One worked example pairing an accepted answer with a rejected one.

    The rejected half is the point. Rule 2 of the evidence contract already
    forbids inventing evidence ids, yet a plausible-looking citation borrowed
    from the model's own pretraining is exactly the output that slips through:
    it reads correct, so nothing in the prompt feels violated. Showing the
    concrete failure -- a well-formed risk whose id simply is not in the
    context, and the note that the verifier quarantines it -- makes the rule
    operational rather than aspirational.
    """

    context: str
    good: str
    bad: str
    rejection: str

    def render(self) -> str:
        """Return the example as a labelled block for inclusion in a prompt."""
        return (
            f"Context evidence:\n{self.context.strip()}\n"
            f"ACCEPTED answer:\n{self.good.strip()}\n"
            f"REJECTED answer:\n{self.bad.strip()}\n"
            f"Why it is rejected: {self.rejection.strip()}"
        )


#: Header introducing the worked examples, kept separate so tests can assert on
#: it without pinning the example bodies themselves.
FEW_SHOT_HEADER = (
    "Worked examples. The REJECTED answers below are the exact failure mode to "
    "avoid -- they look plausible but cite evidence that is not in the context:"
)


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
    examples: tuple[FewShotExample, ...] = ()

    def render(self, **values: object) -> str:
        """Return the full prompt (system preamble + examples + filled body).

        Examples sit between the contract and the task so the model reads the
        rule, sees it applied, and only then meets the real context.

        Raises:
            KeyError: if the template references a placeholder not provided.
        """
        rendered = Template(self.body).substitute(
            {key: str(value) for key, value in values.items()}
        )
        sections = [self.system]
        if self.examples:
            blocks = "\n\n".join(example.render() for example in self.examples)
            sections.append(f"{FEW_SHOT_HEADER}\n\n{blocks}")
        sections.append(rendered.strip())
        return "\n\n".join(sections)


#: ``v2`` replaced the hand-curated "skill patterns" block with the two signals
#: that exist for every dependency: the API symbols the code scan found and the
#: documentation the shared corpus retrieved. Planning no longer depends on a
#: dedicated Skill Pack.
PLANNER = PromptTemplate(
    name="planner",
    version="v2",
    body="""\
You are planning an upgrade impact analysis.
Target dependency: $dependency
Source version (from-version): $source_version
API symbols of the dependency that this repository actually uses:
$code_symbols
Documentation retrieved for this upgrade:
$doc_evidence
Collected code evidence:
$code_evidence
Return a plan listing the topics worth inspecting, with one question each. Use
the API symbol as the topic id when the topic is about a symbol above; otherwise
use a short slug derived from the documentation heading.""",
)

#: Code evidence exists, documentation does not. The tempting move is to fill
#: the gap from memory; the correct move is to report the usage and admit the
#: impact is unconfirmed.
_UNDOCUMENTED_PATTERN_EXAMPLE = FewShotExample(
    context=(
        "- [code:sqlalchemy:3ab19c0d5f21] (code_usage) call Query.get at app/dao.py:44\n"
        "  session.query(User).get(user_id)"
    ),
    good=(
        '{"pattern_id": "Query.get", "title": "Query.get() called in app/dao.py", '
        '"detail": "app/dao.py:44 calls Query.get(). The context carries no '
        "documentation about this API in the target version, so the impact is "
        'unconfirmed.", "severity": "low", '
        '"evidence_ids": ["code:sqlalchemy:3ab19c0d5f21"]}'
    ),
    bad=(
        '{"pattern_id": "Query.get", "title": "Query.get() is removed in 2.0", '
        '"detail": "The 2.0 migration guide replaces Query.get() with '
        'Session.get().", "severity": "high", '
        '"evidence_ids": ["code:sqlalchemy:3ab19c0d5f21", "doc:sqlalchemy-migration:14"]}'
    ),
    rejection=(
        "doc:sqlalchemy-migration:14 never appears in the context -- it was recalled "
        "from pretraining, not retrieved for this run. The verifier drops the whole "
        "record, so the real usage is lost too. Cite only the code evidence and keep "
        "the severity low until documentation confirms the change."
    ),
)

#: The classic over-reach: one well-grounded risk, then a second one that is
#: true in general but unsupported here.
_UNGROUNDED_RISK_EXAMPLE = FewShotExample(
    context=(
        "- [code:pydantic:9f2c1a4b7e33] (code_usage) decorator validator at app/models.py:12\n"
        '  @validator("email")\n'
        "- [doc:pydantic-migration:7] (doc_chunk) pydantic-migration chunk 7 "
        "(Migration/Validators)\n"
        "  @validator is removed in v2; use @field_validator instead."
    ),
    good=(
        '{"risks": [{"risk_id": "risk:1", "title": "@validator is removed in v2", '
        '"severity": "high", "confidence": "high", '
        '"evidence_ids": ["code:pydantic:9f2c1a4b7e33", "doc:pydantic-migration:7"], '
        '"recommendation": "Replace @validator with @field_validator in app/models.py."}], '
        '"notes": ""}'
    ),
    bad=(
        '{"risks": [{"risk_id": "risk:1", "title": "@validator is removed in v2", '
        '"severity": "high", "confidence": "high", '
        '"evidence_ids": ["code:pydantic:9f2c1a4b7e33", "doc:pydantic-migration:7"], '
        '"recommendation": "Replace @validator with @field_validator in app/models.py."}, '
        '{"risk_id": "risk:2", "title": "BaseSettings moved to pydantic-settings", '
        '"severity": "high", "confidence": "high", '
        '"evidence_ids": ["doc:pydantic-migration:19"], '
        '"recommendation": "Install pydantic-settings."}], "notes": ""}'
    ),
    rejection=(
        "risk:2 cites doc:pydantic-migration:19, which is not in the context, and "
        "nothing shows this repository uses BaseSettings at all. A widely known fact "
        "is still a hallucination when this run produced no evidence for it. Omit the "
        "risk entirely rather than adding it for completeness."
    ),
)

#: ``v2`` adds the few-shot pair above. The body is unchanged, so any behaviour
#: difference between v1 and v2 is attributable to the examples alone.
BREAKING_CHANGE = PromptTemplate(
    name="breaking_change",
    version="v2",
    body="""\
Analyze the potential breaking change for pattern '$pattern_id'.
Question: $question

Context evidence:
$context

Return a BreakingChange. Reference only evidence ids present in the context.""",
    examples=(_UNDOCUMENTED_PATTERN_EXAMPLE,),
)

#: ``v2`` adds the few-shot pair above; the body is unchanged (see BREAKING_CHANGE).
IMPACT_REPORT = PromptTemplate(
    name="impact_report",
    version="v2",
    body="""\
Produce the final upgrade impact report for '$dependency'.
Source version (from-version): $source_version
Plan:
$plan
Breaking changes:
$breaking_changes
Context evidence:
$context

Return an ImpactReport. Every risk MUST reference only evidence ids that appear
in the context.""",
    examples=(_UNGROUNDED_RISK_EXAMPLE,),
)

ROUTER = PromptTemplate(
    name="router",
    version=PROMPT_VERSION,
    body="""\
Classify the user's request and extract the three elements needed to run a
dependency-upgrade impact assessment.

User request:
$user_text

Return an Intent with:
- kind: one of upgrade_task / not_upgrade / invalid_url / need_clarification
- repo: the GitHub repository URL or local path, only if one is present
- dependency: the Python package being upgraded, only if present
- target_version: the version being upgraded TO, only if present
- source_version: the current version being upgraded FROM, only if present
- missing: which of repo / dependency / target_version are absent
- confidence: your confidence in this classification, between 0 and 1""",
)

#: Used by :mod:`upgradelens.llm.query_rewrite` in ``live`` mode to expand the
#: structured upgrade intent into several documentation search queries.
QUERY_REWRITER = PromptTemplate(
    name="query_rewriter",
    version=PROMPT_VERSION,
    body="""\
Expand the user's upgrade-assessment request into the search queries most
likely to retrieve the relevant documentation sections.

Target dependency: $package
Upgrading FROM: $source_version (may be empty)
Upgrading TO: $target_version (may be empty)
Original user intent: $user_intent
API symbols this repository actually uses:
$code_symbols

Return 2-5 short search queries. Each query should be a concise phrase a
documentation search box would accept: include the dependency name where
helpful, version-specific terms (e.g. "v2 migration"), and the API symbols
above. Prefer concrete API names and migration keywords over vague wording.
Do not invent versions or APIs that are not implied by the inputs.""",
)

PROMPTS: dict[str, PromptTemplate] = {
    template.name: template
    for template in (PLANNER, BREAKING_CHANGE, IMPACT_REPORT, ROUTER, QUERY_REWRITER)
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
    "FEW_SHOT_HEADER",
    "IMPACT_REPORT",
    "FewShotExample",
    "PLANNER",
    "ROUTER",
    "PROMPTS",
    "PROMPT_VERSION",
    "PromptTemplate",
    "QUERY_REWRITER",
    "get_prompt",
]
