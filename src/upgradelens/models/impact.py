"""Stage 5 structured-output models and evidence bundling.

These models are the contract between the model gateway and the minimal
LangGraph loop. Every :class:`RiskItem` produced by the loop must reference
only evidence ids that exist in the :class:`EvidenceBundle`, so that the model
cannot invent evidence.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeUsage
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.domain.skill import SkillPackage

Severity = Literal["high", "medium", "low"]
Confidence = Literal["high", "low"]

_CODE_KINDS = ("code_usage", "parse_error", "dynamic_import")


def _rel(path: str) -> str:
    return os.path.basename(path)


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _usage_id(dependency: str, usage: CodeUsage) -> str:
    raw = f"{usage.path}:{usage.start_line}:{usage.kind}:{usage.symbol}"
    return f"code:{dependency}:{_short_hash(raw)}"


def _count_kinds(bundle: EvidenceBundle) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in bundle.items:
        summary[item.kind] = summary.get(item.kind, 0) + 1
    return summary


@dataclass(frozen=True)
class EvidenceItem:
    """A single, addressable piece of evidence fed to the model."""

    evidence_id: str
    kind: str  # "code_usage" | "parse_error" | "dynamic_import" | "doc_chunk"
    summary: str
    detail: str
    meta: dict[str, Any] = field(default_factory=dict)


class EvidenceBundle:
    """An ordered, de-duplicated collection of :class:`EvidenceItem`."""

    def __init__(self, items: list[EvidenceItem] | None = None) -> None:
        self._items: list[EvidenceItem] = []
        self._by_id: dict[str, EvidenceItem] = {}
        for item in items or []:
            self.add(item)

    @property
    def items(self) -> list[EvidenceItem]:
        return list(self._items)

    def add(self, item: EvidenceItem) -> None:
        if item.evidence_id in self._by_id:
            return
        self._items.append(item)
        self._by_id[item.evidence_id] = item

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return self._by_id.get(evidence_id)

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self._by_id

    @property
    def ids(self) -> set[str]:
        return set(self._by_id)

    def by_kind(self, kind: str) -> list[EvidenceItem]:
        return [it for it in self._items if it.kind == kind]

    def as_context(self, *, max_detail_chars: int = 400) -> str:
        lines: list[str] = []
        for it in self._items:
            detail = it.detail
            if len(detail) > max_detail_chars:
                detail = detail[:max_detail_chars] + " …"
            lines.append(f"- [{it.evidence_id}] ({it.kind}) {it.summary}\n  {detail}")
        return "\n".join(lines)


class PlanItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern_id: str = ""
    question: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[PlanItem] = Field(default_factory=list)


class BreakingChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern_id: str = ""
    title: str = ""
    detail: str = ""
    severity: Severity = "low"
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> BreakingChange:
        if not self.title.strip():
            raise ValueError("BreakingChange.title must not be empty")
        return self


class RiskItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_id: str
    title: str
    severity: Severity = "low"
    confidence: Confidence = "low"
    evidence_ids: list[str] = Field(default_factory=list)
    recommendation: str = ""

    @model_validator(mode="after")
    def _check(self) -> RiskItem:
        if not self.risk_id.strip():
            raise ValueError("RiskItem.risk_id must not be empty")
        if not self.title.strip():
            raise ValueError("RiskItem.title must not be empty")
        return self


class ImpactReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "impact-report/1"
    target_dependency: str = ""
    source_version_spec: str = ""
    target_version_spec: str = ""
    generated_at: str = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC).isoformat())
    risks: list[RiskItem] = Field(default_factory=list)
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    static: bool = False
    notes: str = ""


def build_bundle(
    code_report: CodeEvidenceReport,
    doc_runs: list[RetrievalRun] | None = None,
    *,
    dependency: str,
) -> EvidenceBundle:
    """Assemble every addressable evidence item for the closed loop."""
    bundle = EvidenceBundle()
    for usage in code_report.usages:
        bundle.add(
            EvidenceItem(
                evidence_id=_usage_id(dependency, usage),
                kind="code_usage",
                summary=f"{usage.kind} {usage.symbol} at {usage.path}:{usage.start_line}",
                detail=(usage.snippet or "").strip(),
                meta={
                    "path": usage.path,
                    "line": usage.start_line,
                    "usage_kind": str(usage.kind),
                    "symbol": usage.symbol,
                },
            )
        )
    for perr in code_report.parse_errors:
        rel = _rel(perr.path)
        pid = f"code:{dependency}:parse:{_short_hash(perr.path)}"
        bundle.add(
            EvidenceItem(
                evidence_id=pid,
                kind="parse_error",
                summary=f"parse error in {rel}: {perr.message}",
                detail=f"{rel}: {perr.message}",
                meta={"path": perr.path},
            )
        )
    for dyn in code_report.dynamic_imports:
        rel = _rel(dyn.path)
        did = f"code:{dependency}:dyn:{_short_hash(f'{dyn.path}:{dyn.line}:{dyn.mechanism}')}"
        bundle.add(
            EvidenceItem(
                evidence_id=did,
                kind="dynamic_import",
                summary=f"dynamic import ({dyn.mechanism}) in {rel}",
                detail=(dyn.snippet or "").strip(),
                meta={"path": dyn.path, "line": dyn.line, "mechanism": dyn.mechanism},
            )
        )
    for run in doc_runs or []:
        for chunk_id, de in zip(run.matched_chunk_ids, run.top_doc_evidence, strict=False):
            heading = "/".join(de.heading_path)
            bundle.add(
                EvidenceItem(
                    evidence_id=f"doc:{de.source_id}:{chunk_id}",
                    kind="doc_chunk",
                    summary=f"{de.source_id} chunk {chunk_id} ({heading})",
                    detail=de.snippet,
                    meta={
                        "source_id": de.source_id,
                        "chunk_id": chunk_id,
                        "heading_path": heading,
                        "url": de.url,
                    },
                )
            )
    return bundle


def build_static_report(
    bundle: EvidenceBundle,
    skill: SkillPackage | None = None,
    *,
    dependency: str = "",
    source_version_spec: str = "",
    target_version_spec: str = "",
    notes: str = "",
) -> ImpactReport:
    """Deterministic, model-free fallback report.

    Risks are derived from code evidence only and every risk references a real
    evidence id, so the static report is still safe to surface.
    """
    risks: list[RiskItem] = []
    counter = 0
    for item in bundle.items:
        if item.kind not in _CODE_KINDS:
            continue
        counter += 1
        severity: Severity = "high" if item.kind in ("parse_error", "dynamic_import") else "low"
        risks.append(
            RiskItem(
                risk_id=f"risk:{counter}",
                title=item.summary,
                severity=severity,
                confidence="low",
                evidence_ids=[item.evidence_id],
                recommendation=("Review this usage against the target version migration guide."),
            )
        )

    note = notes or (
        "Static fallback: model gateway unavailable; risks derived from code evidence only."
    )
    return ImpactReport(
        target_dependency=dependency,
        source_version_spec=source_version_spec,
        target_version_spec=target_version_spec,
        risks=risks,
        evidence_summary=_count_kinds(bundle),
        static=True,
        notes=note,
    )
