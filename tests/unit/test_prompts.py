"""Tests for the externalised prompt templates and the evidence contract.

The templates are the only place the model is told what it may cite, so these
tests pin the two properties that matter: the contract is always prepended, and
rendering is strict (a missing placeholder fails loudly instead of shipping a
half-filled prompt to a paid API).
"""

from __future__ import annotations

import pytest

from upgradelens.llm.prompts import (
    BREAKING_CHANGE,
    EVIDENCE_CONTRACT,
    IMPACT_REPORT,
    PLANNER,
    PROMPTS,
    PromptTemplate,
    get_prompt,
)


def test_every_template_prepends_the_evidence_contract() -> None:
    rendered = [
        PLANNER.render(dependency="pydantic", patterns="- p1", code_evidence="- x"),
        BREAKING_CHANGE.render(pattern_id="p1", question="q?", context="[e1] ..."),
        IMPACT_REPORT.render(
            dependency="pydantic", plan="- p1", breaking_changes="- bc", context="[e1] ..."
        ),
    ]
    for text in rendered:
        assert text.startswith(EVIDENCE_CONTRACT)


def test_contract_states_the_no_invented_ids_rule() -> None:
    lowered = EVIDENCE_CONTRACT.lower()
    assert "evidence id" in lowered
    assert "invent" in lowered


def test_render_substitutes_every_placeholder() -> None:
    text = PLANNER.render(dependency="pydantic", patterns="- p1", code_evidence="- usage")
    assert "Target dependency: pydantic" in text
    assert "- usage" in text
    assert "$dependency" not in text
    assert "$patterns" not in text
    assert "$code_evidence" not in text


def test_render_rejects_missing_placeholder() -> None:
    with pytest.raises(KeyError):
        PLANNER.render(dependency="pydantic")


def test_values_with_braces_survive_rendering() -> None:
    snippet = 'model_config = {"populate_by_name": True}'
    text = BREAKING_CHANGE.render(pattern_id="p1", question="q?", context=snippet)
    assert snippet in text


def test_registry_lookup() -> None:
    assert set(PROMPTS) == {"planner", "breaking_change", "impact_report", "router"}
    assert get_prompt("planner") is PLANNER
    with pytest.raises(KeyError):
        get_prompt("nope")


def test_templates_are_versioned() -> None:
    for template in PROMPTS.values():
        assert isinstance(template, PromptTemplate)
        assert template.version
