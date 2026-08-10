"""Rule-based risk severity aggregation (plan section 13.3).

Severity is *not* taken from the model's own score. It is recomputed from
factors that a reviewer can inspect and argue with: how big the version jump
is, how many files are touched, whether the code is production or test, whether
tests exist, how trustworthy the documentation is, and how certain the evidence
is.

The key safety property: high *uncertainty* must never be rendered as high
*risk*. Degraded evidence statuses are capped, see :func:`score_risk`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from upgradelens.domain.skill import SkillPackage
from upgradelens.models.impact import EvidenceItem
from upgradelens.verify.models import EvidenceStatus, RiskFactor

__all__ = ["RiskScoringInput", "score_risk", "is_major_bump"]

_HIGH_THRESHOLD = 7
_MEDIUM_THRESHOLD = 4

_TRUST_POINTS = {"official": 1, "community": 0, "unverified": 0}

# Wording in the retrieved documentation that indicates a hard API break rather
# than a soft behaviour change.
_BREAKING_WORDS = ("removed", "renamed", "deleted", "no longer", "replaced by", "must be")
_BEHAVIOUR_WORDS = ("behaviour", "behavior", "default", "changed", "semantics")

#: Symbols shorter than this are too generic to be searched for in prose.
_MIN_SYMBOL_LEN = 3

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?")


def _first_major(spec: str) -> int | None:
    """Return the major number of the first version mentioned in ``spec``.

    ``spec`` is a PEP 440-ish specifier such as ``"<2.0"`` or ``">=2,<3"``; we
    only need a coarse comparison, so a full parse would be overkill and would
    fail on the looser strings that appear in skill metadata.
    """
    match = _VERSION_RE.search(spec or "")
    if match is None:
        return None
    return int(match.group(1))


def is_major_bump(source_spec: str, target_spec: str) -> bool:
    """True when the two specifiers clearly cross a major version boundary."""
    source_major = _first_major(source_spec)
    target_major = _first_major(target_spec)
    if source_major is None or target_major is None:
        return False
    return target_major > source_major


@dataclass(frozen=True)
class RiskScoringInput:
    """Everything the rule engine is allowed to look at.

    Every factor is derived from evidence. ``skill`` is retained only as a
    fallback for legacy doc evidence that carries no ``trust_level`` meta; a
    missing Skill Pack never lowers the quality of the scoring.
    """

    status: EvidenceStatus
    code_items: list[EvidenceItem] = field(default_factory=list)
    doc_items: list[EvidenceItem] = field(default_factory=list)
    skill: SkillPackage | None = None
    source_version_spec: str = ""
    target_version_spec: str = ""
    has_recommended_tests: bool = False
    risk_title: str = ""


def _cited_symbols(data: RiskScoringInput) -> set[str]:
    symbols = {str(item.meta.get("symbol", "")) for item in data.code_items}
    symbols.discard("")
    return {s for s in symbols if len(s) >= _MIN_SYMBOL_LEN}


def _doc_text(data: RiskScoringInput) -> str:
    """All prose of the cited documentation, lower-cased for keyword matching."""
    return " ".join(f"{item.summary} {item.detail}" for item in data.doc_items).lower()


def _doc_grounding_points(data: RiskScoringInput) -> tuple[int, str]:
    """How firmly the retrieved documentation lands on the API this code uses.

    Documentation that explicitly names the symbol found in the repository is
    the strongest deterministic signal that the upgrade really touches this
    code; retrieved-but-unrelated documentation is much weaker; no documentation
    scores nothing. This replaces the old skill-pattern severity lookup, which
    was unavailable for dependencies without a dedicated Skill Pack.
    """
    if not data.doc_items:
        return 0, "no doc evidence"
    haystack = _doc_text(data)
    grounded = sorted(s for s in _cited_symbols(data) if s.lower() in haystack)
    if grounded:
        return 3, "docs name " + ", ".join(grounded[:3])
    return 1, "docs retrieved, no symbol overlap"


def _api_change_points(data: RiskScoringInput) -> tuple[int, str]:
    """Distinguish a hard API break from a softer behaviour change.

    The wording is read from the risk title plus the documentation actually
    cited, rather than from a hand-written ``risk_hint`` on a skill pattern.
    """
    haystack = data.risk_title.lower() + " " + _doc_text(data)
    if any(word in haystack for word in _BREAKING_WORDS):
        return 2, "api removed/renamed"
    if any(word in haystack for word in _BEHAVIOUR_WORDS):
        return 1, "behaviour/config change"
    return 0, "no explicit break wording"


def _doc_trust(data: RiskScoringInput) -> tuple[int, str]:
    """Trust level of the cited documentation, taken from the evidence itself.

    The shared corpus stamps every chunk with its ``trust_level``; the Skill
    lookup only covers legacy evidence that predates that meta field.
    """
    if not data.doc_items:
        return 0, "no doc evidence"
    levels = [str(item.meta.get("trust_level", "") or "") for item in data.doc_items]
    levels = [level for level in levels if level]
    if not levels and data.skill is not None:
        source_ids = {str(item.meta.get("source_id", "")) for item in data.doc_items}
        levels = [s.trust_level for s in data.skill.sources if s.id in source_ids]
    label = ("official" if "official" in levels else levels[0]) if levels else "unverified"
    return _TRUST_POINTS.get(label, 0), label


def score_risk(data: RiskScoringInput) -> tuple[int, str, list[RiskFactor]]:
    """Return ``(score, severity, factors)`` for one risk.

    Every factor that contributed is returned, including the zero-point ones,
    so the report can explain exactly why a severity was chosen.
    """
    factors: list[RiskFactor] = []

    major = is_major_bump(data.source_version_spec, data.target_version_spec)
    factors.append(
        RiskFactor(
            name="major_version_bump",
            value=f"{data.source_version_spec or '?'} -> {data.target_version_spec or '?'}",
            points=3 if major else 0,
        )
    )

    grounding_points, grounding_label = _doc_grounding_points(data)
    factors.append(
        RiskFactor(name="doc_symbol_grounding", value=grounding_label, points=grounding_points)
    )

    api_points, api_label = _api_change_points(data)
    factors.append(RiskFactor(name="api_change_kind", value=api_label, points=api_points))

    paths = {str(item.meta.get("path", "")) for item in data.code_items}
    paths.discard("")
    file_points = 2 if len(paths) >= 3 else (1 if paths else 0)
    factors.append(RiskFactor(name="impacted_files", value=str(len(paths)), points=file_points))

    production = [item for item in data.code_items if not item.meta.get("is_test_code", False)]
    test_only = bool(data.code_items) and not production
    factors.append(
        RiskFactor(
            name="production_code",
            value="yes" if production else "test-only",
            points=2 if production else -2,
        )
    )

    factors.append(
        RiskFactor(
            name="test_coverage",
            value="tests found" if data.has_recommended_tests else "no related test",
            points=-1 if data.has_recommended_tests else 0,
        )
    )

    trust_points, trust_label = _doc_trust(data)
    factors.append(RiskFactor(name="doc_trust", value=trust_label, points=trust_points))

    dynamic = [item for item in data.code_items if item.kind == "dynamic_import"]
    factors.append(
        RiskFactor(
            name="dynamic_usage",
            value="present" if dynamic else "none",
            points=1 if dynamic else 0,
        )
    )

    uncertain = data.status in (
        EvidenceStatus.INSUFFICIENT_EVIDENCE,
        EvidenceStatus.CONFLICTING_EVIDENCE,
    )
    factors.append(
        RiskFactor(
            name="evidence_status",
            value=str(data.status),
            points=-2 if uncertain else 0,
        )
    )

    score = sum(f.points for f in factors)

    if score >= _HIGH_THRESHOLD:
        severity = "high"
    elif score >= _MEDIUM_THRESHOLD:
        severity = "medium"
    else:
        severity = "low"

    # Ceilings. These are policy, not arithmetic, so they are applied after the
    # score and stated explicitly rather than being tuned into the weights.
    if data.status is EvidenceStatus.NOT_APPLICABLE:
        severity = "low"
    elif test_only:
        # A break confined to the test suite is real but cannot take production
        # down; it must never outrank an actual production risk.
        severity = "low"
    elif uncertain and severity == "high":
        # High uncertainty is not the same thing as high risk.
        severity = "medium"

    return score, severity, factors
