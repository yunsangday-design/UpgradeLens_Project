"""Tests for the externalised prompt templates and the evidence contract.

The templates are the only place the model is told what it may cite, so these
tests pin the two properties that matter: the contract is always prepended, and
rendering is strict (a missing placeholder fails loudly instead of shipping a
half-filled prompt to a paid API).
"""

from __future__ import annotations

import json

import pytest

from upgradelens.llm.prompts import (
    BREAKING_CHANGE,
    EVIDENCE_CONTRACT,
    FEW_SHOT_HEADER,
    IMPACT_REPORT,
    PLANNER,
    PROMPTS,
    FewShotExample,
    PromptTemplate,
    get_prompt,
)
from upgradelens.models.impact import BreakingChange, ImpactReport


def test_every_template_prepends_the_evidence_contract() -> None:
    rendered = [
        PLANNER.render(
            dependency="pydantic",
            source_version="",
            code_symbols="- validator",
            doc_evidence="- [doc:1] Validators",
            code_evidence="- x",
        ),
        BREAKING_CHANGE.render(pattern_id="p1", question="q?", context="[e1] ..."),
        IMPACT_REPORT.render(
            dependency="pydantic",
            source_version="",
            plan="- p1",
            breaking_changes="- bc",
            context="[e1] ...",
        ),
    ]
    for text in rendered:
        assert text.startswith(EVIDENCE_CONTRACT)


def test_contract_states_the_no_invented_ids_rule() -> None:
    lowered = EVIDENCE_CONTRACT.lower()
    assert "evidence id" in lowered
    assert "invent" in lowered


def test_render_substitutes_every_placeholder() -> None:
    text = PLANNER.render(
        dependency="pydantic",
        source_version="",
        code_symbols="- validator",
        doc_evidence="- [doc:1] Validators",
        code_evidence="- usage",
    )
    assert "Target dependency: pydantic" in text
    assert "- usage" in text
    assert "- validator" in text
    assert "- [doc:1] Validators" in text
    assert "$dependency" not in text
    assert "$code_symbols" not in text
    assert "$doc_evidence" not in text
    assert "$code_evidence" not in text


def test_render_rejects_missing_placeholder() -> None:
    with pytest.raises(KeyError):
        PLANNER.render(dependency="pydantic")


def test_values_with_braces_survive_rendering() -> None:
    snippet = 'model_config = {"populate_by_name": True}'
    text = BREAKING_CHANGE.render(pattern_id="p1", question="q?", context=snippet)
    assert snippet in text


def test_registry_lookup() -> None:
    assert set(PROMPTS) == {
        "planner",
        "breaking_change",
        "impact_report",
        "router",
        "query_rewriter",
    }
    assert get_prompt("planner") is PLANNER
    with pytest.raises(KeyError):
        get_prompt("nope")


def test_templates_are_versioned() -> None:
    for template in PROMPTS.values():
        assert isinstance(template, PromptTemplate)
        assert template.version


# --- few-shot examples ----------------------------------------------------
#
# The examples are prompt *content*, so they cannot be tested by behaviour.
# What can be tested is that they are internally honest: the accepted answer
# must cite only ids present in its own context, and the rejected answer must
# actually commit the sin it is being rejected for. Without these checks a
# well-meaning edit could silently turn the counter-example into a valid one
# and teach the model the opposite lesson.

#: Templates whose schema contains ``evidence_ids`` and therefore can hallucinate one.
_CITING_TEMPLATES = [BREAKING_CHANGE, IMPACT_REPORT]


def _cited_ids(answer: str) -> list[str]:
    """Collect every id under any ``evidence_ids`` key in an example answer."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "evidence_ids" and isinstance(value, list):
                    found.extend(str(item) for item in value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(answer))
    return found


@pytest.mark.parametrize("template", _CITING_TEMPLATES, ids=lambda t: t.name)
def test_citing_templates_ship_a_counter_example(template: PromptTemplate) -> None:
    assert template.examples, f"{template.name} emits evidence_ids but shows no example"


@pytest.mark.parametrize("template", _CITING_TEMPLATES, ids=lambda t: t.name)
def test_accepted_answers_cite_only_context_ids(template: PromptTemplate) -> None:
    for example in template.examples:
        cited = _cited_ids(example.good)
        assert cited, "the accepted answer should demonstrate citing evidence"
        for evidence_id in cited:
            assert evidence_id in example.context


@pytest.mark.parametrize("template", _CITING_TEMPLATES, ids=lambda t: t.name)
def test_rejected_answers_cite_an_id_absent_from_the_context(
    template: PromptTemplate,
) -> None:
    for example in template.examples:
        invented = [eid for eid in _cited_ids(example.bad) if eid not in example.context]
        assert invented, "the rejected answer must actually invent an id"
        assert example.rejection.strip()


def test_example_answers_validate_against_the_real_schemas() -> None:
    # Both halves must parse: the rejected answer is rejected for being
    # ungrounded, not for being malformed. That is precisely why the prompt
    # cannot rely on schema validation to catch it.
    for example in BREAKING_CHANGE.examples:
        BreakingChange.model_validate_json(example.good)
        BreakingChange.model_validate_json(example.bad)
    for example in IMPACT_REPORT.examples:
        ImpactReport.model_validate_json(example.good)
        ImpactReport.model_validate_json(example.bad)


def test_examples_render_between_the_contract_and_the_task() -> None:
    text = IMPACT_REPORT.render(
        dependency="pydantic",
        source_version="",
        plan="- p1",
        breaking_changes="- bc",
        context="[e1] ...",
    )
    assert text.startswith(EVIDENCE_CONTRACT)
    header_at = text.index(FEW_SHOT_HEADER)
    task_at = text.index("Produce the final upgrade impact report")
    assert len(EVIDENCE_CONTRACT) <= header_at < task_at
    assert "ACCEPTED answer:" in text
    assert "REJECTED answer:" in text


def test_templates_without_examples_render_unchanged() -> None:
    text = PLANNER.render(
        dependency="pydantic",
        source_version="",
        code_symbols="- validator",
        doc_evidence="- [doc:1] Validators",
        code_evidence="- usage",
    )
    assert FEW_SHOT_HEADER not in text
    assert "ACCEPTED answer:" not in text


def test_example_text_is_never_placeholder_substituted() -> None:
    # Only the body goes through string.Template; examples are literal text and
    # may legitimately contain "$" (shell snippets, prices, regexes).
    template = PromptTemplate(
        name="t",
        version="v1",
        body="Body: $value",
        examples=(FewShotExample(context="[e1] cost is $5", good="{}", bad="{}", rejection="why"),),
    )
    text = template.render(value="filled")
    assert "cost is $5" in text
    assert "Body: filled" in text
