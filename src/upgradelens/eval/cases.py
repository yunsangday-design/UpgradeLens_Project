"""Loading of offline evaluation cases (plan section 18.1).

A case is a directory containing a miniature repository plus the expectations
it must satisfy. Cases are pure data on disk so they can be reviewed, diffed and
extended without touching Python.

Layout::

    <case_dir>/
        case.yaml            # metadata + expectations
        repo/                # the repository under analysis
        model_report.json    # optional synthetic model output

Evidence ids are content hashes, so ``model_report.json`` cannot hard-code them.
It uses placeholders instead -- see :func:`resolve_placeholders`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from upgradelens.models.impact import EvidenceBundle

__all__ = ["EvalCase", "Expectation", "load_cases", "load_case", "resolve_placeholders"]

_CODE_PLACEHOLDER = re.compile(r"^\{\{code:(?P<path>[^:]+):(?P<symbol>[^}]+)\}\}$")
_DOC_PLACEHOLDER = re.compile(r"^\{\{doc:(?P<index>\d+)\}\}$")


@dataclass(frozen=True)
class Expectation:
    """What a correct system must produce for a case."""

    conclusion: str | None = None
    min_verified_risks: int | None = None
    max_verified_risks: int | None = None
    must_cite_paths: list[str] = field(default_factory=list)
    must_flag_symbols: list[str] = field(default_factory=list)
    must_quarantine_risk_ids: list[str] = field(default_factory=list)
    partial: bool | None = None
    max_severity: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Expectation:
        return cls(
            conclusion=data.get("conclusion"),
            min_verified_risks=data.get("min_verified_risks"),
            max_verified_risks=data.get("max_verified_risks"),
            must_cite_paths=list(data.get("must_cite_paths") or []),
            must_flag_symbols=list(data.get("must_flag_symbols") or []),
            must_quarantine_risk_ids=list(data.get("must_quarantine_risk_ids") or []),
            partial=data.get("partial"),
            max_severity=data.get("max_severity"),
        )


@dataclass(frozen=True)
class EvalCase:
    """One evaluation scenario."""

    case_id: str
    root: Path
    dependency: str
    target_version: str
    source_version: str = ""
    skill_id: str = ""
    description: str = ""
    with_docs: bool = True
    model_report_path: Path | None = None
    expect: Expectation = field(default_factory=Expectation)

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    def load_model_report(self) -> dict[str, Any] | None:
        """Return the synthetic model output, if this case ships one."""
        if self.model_report_path is None:
            return None
        with open(self.model_report_path, encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
        return data


def load_case(case_dir: Path) -> EvalCase:
    """Load a single case directory."""
    meta_path = case_dir / "case.yaml"
    with open(meta_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    model_report = data.get("model_report")
    model_report_path = case_dir / model_report if model_report else None
    if model_report_path is not None and not model_report_path.is_file():
        raise FileNotFoundError(f"{meta_path}: model_report '{model_report}' not found")

    return EvalCase(
        case_id=str(data.get("case_id") or case_dir.name),
        root=case_dir,
        dependency=str(data["dependency"]),
        target_version=str(data["target_version"]),
        source_version=str(data.get("source_version") or ""),
        skill_id=str(data.get("skill_id") or ""),
        description=str(data.get("description") or "").strip(),
        with_docs=bool(data.get("with_docs", True)),
        model_report_path=model_report_path,
        expect=Expectation.from_dict(data.get("expect") or {}),
    )


def load_cases(base_dir: Path) -> list[EvalCase]:
    """Load every case under ``base_dir``, sorted by id for stable reports."""
    cases = [
        load_case(child)
        for child in sorted(Path(base_dir).iterdir())
        if child.is_dir() and (child / "case.yaml").is_file()
    ]
    if not cases:
        raise ValueError(f"no evaluation cases found under {base_dir}")
    return sorted(cases, key=lambda c: c.case_id)


def resolve_placeholders(evidence_ids: list[str], bundle: EvidenceBundle) -> list[str]:
    """Expand ``{{code:path:symbol}}`` / ``{{doc:N}}`` against a real bundle.

    Anything that is not a placeholder is passed through untouched -- that is
    how a case injects a deliberately fabricated id. A placeholder that matches
    nothing is dropped, because leaving the raw ``{{...}}`` text in would show
    up as a fake "hallucination" that the case never intended.
    """
    docs = bundle.by_kind("doc_chunk")
    out: list[str] = []
    for raw in evidence_ids:
        code_match = _CODE_PLACEHOLDER.match(raw)
        if code_match is not None:
            wanted_path = code_match.group("path")
            wanted_symbol = code_match.group("symbol")
            for item in bundle.by_kind("code_usage"):
                if (
                    item.meta.get("path") == wanted_path
                    and item.meta.get("symbol") == wanted_symbol
                ):
                    out.append(item.evidence_id)
                    break
            continue

        doc_match = _DOC_PLACEHOLDER.match(raw)
        if doc_match is not None:
            index = int(doc_match.group("index"))
            if index < len(docs):
                out.append(docs[index].evidence_id)
            continue

        out.append(raw)
    return out
