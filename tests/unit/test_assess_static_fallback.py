"""Static fallback tests: when the live model is unavailable, the loop still
returns a deterministic report whose risks reference real evidence ids.
"""

from __future__ import annotations

from upgradelens.graph import AssessmentSpec, run_assessment
from upgradelens.llm.gateway import (
    ModelConfig,
    ModelGateway,
    ModelMode,
    ModelUnavailableError,
)
from upgradelens.models.impact import EvidenceBundle, EvidenceItem


class _DownTransport:
    def complete(self, prompt: str, schema: type) -> tuple[object, object]:
        raise ModelUnavailableError("model unreachable")


def test_static_fallback_on_unavailable_model() -> None:
    bundle = EvidenceBundle(
        items=[
            EvidenceItem(
                evidence_id="code:pydantic:u1",
                kind="code_usage",
                summary="subclass pydantic.BaseModel",
                detail="class M(pydantic.BaseModel): x: int",
            )
        ]
    )
    gateway = ModelGateway(ModelConfig(mode=ModelMode.LIVE), transport=_DownTransport())
    spec = AssessmentSpec(repo="/repo", dependency="pydantic", target_version_spec="2.0")
    report = run_assessment(spec, bundle, gateway, skill=None)

    assert report.static is True
    assert report.risks, "static report should still surface code evidence"
    for risk in report.risks:
        for eid in risk.evidence_ids:
            assert eid in bundle.ids
