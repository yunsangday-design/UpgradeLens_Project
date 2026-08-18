"""The outcome of executing/validating a proposal (plan stage S1).

A :class:`VerificationResult` is produced after a patch is applied in the sandbox,
tests run, or a scanner re-executes. ``passed`` is derived from the individual
:class:`VerificationCheck` results, never set by the caller -- so a proposal can
only be reported as verified when every check it declared actually passed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["VerificationCheck", "VerificationResult"]


class VerificationCheck(BaseModel):
    """One atomic check a verifier ran against a proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    detail: str = ""
    evidence_id: str | None = None


class VerificationResult(BaseModel):
    """Aggregate verification of one :class:`ActionProposal`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    checks: list[VerificationCheck] = Field(default_factory=list)
    passed: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive_passed(self) -> VerificationResult:
        """True only when at least one check ran and all of them passed."""
        object.__setattr__(self, "passed", bool(self.checks) and all(c.passed for c in self.checks))
        return self
