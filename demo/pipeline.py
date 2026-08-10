"""Headless assess pipeline used by the Streamlit demo.

Kept free of any Streamlit import so the logic can be smoke-tested offline
without a browser. ``demo/app.py`` is a thin UI layer over :func:`run_assess`.

The analysis itself is :mod:`upgradelens.pipeline`, exactly as the CLI and the
MCP server run it -- what lives here is only the part that is genuinely the
demo's own. In ``fake`` mode the model gateway returns empty canned responses,
which makes the closed loop produce a boring, risk-free report. To keep the
demo illustrative while staying fully offline, :func:`_inject_demo_fixtures`
crafts *canned but evidence-anchored* model outputs (via
:func:`upgradelens.llm.fixtures.build_fake_responses`): the risks reference the
**real** code-evidence ids discovered in the target repo, so the verifier can
see a cited source line and the patch generator can fire where a rule matches.
Canned risks never cite a synthetic doc chunk -- that would be "unknown"
evidence at replay time and the verifier would drop the risk (it does *not*
pretend a risk is ``VERIFIED`` without a real documentation index). This is
clearly an illustrative fixture, not real model reasoning.
"""

from __future__ import annotations

from upgradelens.capabilities import TransformationPack
from upgradelens.llm.fixtures import build_fake_responses
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.patch import generate_patch_draft
from upgradelens.pipeline import (
    AssessmentRequest,
    EvidenceCollection,
    analyse,
    collect_evidence,
)
from upgradelens.tools.registry import ToolContext


def run_assess(
    repo: str,
    dependency: str,
    target_version: str,
    mode: str,
    model: str,
    api_key: str,
    base_url: str,
    allow_quality_patch: bool,
    source_version: str | None = None,
    replay_dir: str | None = None,
    recording_dir: str | None = None,
) -> dict[str, object]:
    """Run the full assess pipeline and return a result bundle for rendering."""
    request = AssessmentRequest(
        repo=repo,
        dependency=dependency,
        target_version=target_version,
        source_version=source_version,
    )

    with ToolContext() as ctx:
        collection = collect_evidence(request, ctx)
        # Fake responses must be built *after* collection: they cite the real
        # evidence ids that the scan just discovered.
        fake_responses = _inject_demo_fixtures(collection) if mode == "fake" else {}
        gateway = ModelGateway(
            ModelConfig(
                mode=ModelMode(mode),
                model=model or "qwen-plus",
                api_key=api_key or "",
                base_url=base_url or "",
            ),
            fake_responses=fake_responses or None,
            replay_dir=replay_dir,
            recording_dir=recording_dir,
        )
        outcome = analyse(collection, gateway, ctx)

        draft = None
        skill = outcome.skill
        capability = TransformationPack.from_skill(skill) if skill is not None else None
        if capability is not None and capability.allow_patch_draft():
            draft = generate_patch_draft(
                outcome.repo_path,
                outcome.verified.verified_risks,
                capability,
                outcome.bundle,
                quality_model_available=allow_quality_patch,
            )

    return {
        "code_report": outcome.code_report,
        "skill": skill,
        "bundle": outcome.bundle,
        "report": outcome.report,
        "verified": outcome.verified,
        "draft": draft,
    }


def _inject_demo_fixtures(collection: EvidenceCollection) -> dict[str, object]:
    """Return canned (evidence-anchored) model outputs for fake-mode display.

    Demo fixtures must never be the reason a run fails, so any error here
    degrades to "no canned output" -- the real pipeline then produces its
    (boring but honest) empty report.
    """
    try:
        responses, _injected = build_fake_responses(
            collection.bundle, collection.request.dependency, collection.skill
        )
    except Exception:
        return {}
    return responses
