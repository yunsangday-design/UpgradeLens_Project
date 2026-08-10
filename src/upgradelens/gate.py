"""CI gate over a verified report (ROADMAP 6.1).

The verifier (stage 6 / B3) produces an auditable :class:`VerifiedReport`. This
module turns its conclusion into a CI gate: when the report contains a risk that
is both ``VERIFIED`` and at or above a blocking severity, the upgrade is blocked.

Degraded / unverified findings never block -- they are precisely the cases the
gate must NOT punish. A missing or partial finding is a verification gap, not a
confirmed breakage, and the gate refuses to invent risk from it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from upgradelens.verify.models import VerifiedReport

#: Severities that block an upgrade by default. Matched case-insensitively.
DEFAULT_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})

#: Numeric severity floor used when a report stores severity as a number.
_NUMERIC_BLOCKING_FLOOR = 8


def _normalize_severity(severity: str) -> tuple[str, int | None]:
    """Return ``(lower_text, numeric_or_none)`` for a severity value."""
    text = (severity or "").strip().lower()
    numeric: int | None = None
    try:
        numeric = int(text)
    except ValueError:
        pass
    return text, numeric


def is_blocking_severity(severity: str, *, block_on: Iterable[str]) -> bool:
    """Whether ``severity`` is at or above the blocking threshold ``block_on``."""
    block = {s.strip().lower() for s in block_on}
    text, numeric = _normalize_severity(severity)
    if text in block:
        return True
    if numeric is not None and "8" in block:
        # Numeric severities use the same floor as DEFAULT_BLOCKING_SEVERITIES.
        return numeric >= _NUMERIC_BLOCKING_FLOOR
    return False


@dataclass(frozen=True)
class BlockReason:
    risk_id: str
    title: str
    severity: str


@dataclass
class GateResult:
    block: bool
    reasons: list[BlockReason] = field(default_factory=list)
    checked: int = 0
    verified_blocking: int = 0
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "block": self.block,
            "checked": self.checked,
            "verified_blocking": self.verified_blocking,
            "reasons": [
                {"risk_id": r.risk_id, "title": r.title, "severity": r.severity}
                for r in self.reasons
            ],
            "summary": self.summary,
        }


def gate_report(
    report: VerifiedReport,
    *,
    block_on: Iterable[str] | None = None,
) -> GateResult:
    """Decide whether a verified report blocks an upgrade.

    Only ``VERIFIED`` risks at or above a blocking severity block. Degraded or
    unverified findings are excluded by construction.
    """
    severities = set(DEFAULT_BLOCKING_SEVERITIES) if block_on is None else {s for s in block_on}
    reasons: list[BlockReason] = []
    checked = 0
    for risk in report.verified_risks:
        checked += 1
        if not risk.is_verified:
            continue
        if is_blocking_severity(risk.severity, block_on=severities):
            reasons.append(
                BlockReason(
                    risk_id=risk.risk_id,
                    title=risk.title,
                    severity=risk.severity,
                )
            )
    blocking = len(reasons) > 0
    if blocking:
        summary = (
            f"GATE BLOCKED: {len(reasons)} verified risk(s) at or above blocking "
            f"severity ({', '.join(sorted(severities))})."
        )
    else:
        summary = (
            f"GATE OK: no verified risk at or above blocking severity "
            f"({', '.join(sorted(severities))}); {checked} verified risk(s) checked."
        )
    return GateResult(
        block=blocking,
        reasons=reasons,
        checked=checked,
        verified_blocking=len(reasons),
        summary=summary,
    )
