"""Unified Workbench (plan stage S9).

A single entry point that runs *any* registered capability and normalises its
output into one capability-agnostic :class:`CapabilityRunResult`. The Workbench
front end renders only the generic fields, so adding a new capability never
requires touching the UI -- it just has to return one of the accepted result
shapes and be registered in ``defaults.get_default_capabilities``.

Every capability stays offline-capable: in ``fake`` mode the canned responses
cover all five kinds, so the Workbench can replay any scenario without a network
or an API key.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.capabilities.runner import build_gateway
from upgradelens.core.action import ActionKind, PatchProposal, TestProposal
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.core.verification import VerificationCheck, VerificationResult
from upgradelens.llm.gateway import ModelGateway
from upgradelens.presentation.models import UpgradeFindingView

__all__ = ["CapabilityRunResult", "run_capability", "list_capabilities"]

_SEVERITY_NAMES = {"critical", "high", "medium", "low", "info"}


def _to_jsonable_safe(x: Any) -> Any:
    """Recursively convert to a JSON-safe structure.

    Some capability results are dataclasses whose nested fields (e.g. ``ToolTrace``)
    cannot be handled by Pydantic's serializer. This walks the structure and falls
    back to ``str`` for any leaf it cannot otherwise represent, so the Workbench
    result is always serializable over HTTP.
    """
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if isinstance(x, dict):
        return {str(k): _to_jsonable_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_to_jsonable_safe(v) for v in x]
    if isinstance(x, BaseModel):
        try:
            return _to_jsonable_safe(x.model_dump(mode="json"))
        except Exception:
            return _to_jsonable_safe(x.model_dump())
    if is_dataclass(x) and not isinstance(x, type):
        try:
            return _to_jsonable_safe(asdict(x))
        except Exception:
            return str(x)
    if hasattr(x, "model_dump"):
        try:
            return _to_jsonable_safe(x.model_dump(mode="json"))
        except Exception:
            return str(x)
    if hasattr(x, "__dict__"):
        try:
            return _to_jsonable_safe(vars(x))
        except Exception:
            return str(x)
    return str(x)


def _as_list(obj: Any) -> list[Any]:
    """Normalize a possibly-scalar value into a list (``trace`` must stay a list)."""
    if obj is None:
        return []
    if isinstance(obj, (list, tuple, set)):
        return list(obj)
    return [obj]


class CapabilityRunResult(BaseModel):
    """Capability-agnostic result rendered by the unified Workbench.

    Fields are deliberately generic -- no dependency-upgrade-specific naming -- so
    the same view works for PR review, issue repair, security review and breaking
    change analysis alike. ``raw`` carries the full capability-specific dump for
    drill-down; the structured fields are what the default view renders.
    """

    model_config = ConfigDict(extra="allow")

    capability: str = ""
    task: dict[str, Any] = Field(default_factory=dict)
    status: str = "succeeded"  # succeeded | failed
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    action_proposals: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    security_results: dict[str, Any] | None = None
    patch: dict[str, Any] | None = None
    test_results: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    cost: dict[str, Any] = Field(default_factory=dict)
    degradations: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Observability seam filled by :func:`dispatch_by_task` (research report M1b):
    # per-kind capability metadata (name + allowed tools) so the unified result
    # records which engine drove it.
    capability_meta: dict[str, Any] | None = None


def list_capabilities() -> list[dict[str, Any]]:
    """Catalog of capabilities the Workbench can run (kind-agnostic)."""
    from upgradelens.capabilities.defaults import get_default_capabilities

    out: list[dict[str, Any]] = []
    for cap in get_default_capabilities():
        kind = cap.kind.value if isinstance(cap.kind, TaskKind) else str(cap.kind)
        out.append(
            {
                "kind": kind,
                "name": cap.name,
                "description": cap.description,
                "allowed_tools": list(cap.allowed_tools),
            }
        )
    return out


def run_capability(
    task: SoftwareTask,
    *,
    gateway: ModelGateway | None = None,
    mode: str = "fake",
) -> CapabilityRunResult:
    """Run a capability described by ``task`` and normalise its output.

    Exceptions are caught and returned as a ``failed`` result so the Workbench
    never crashes on a single bad run; callers can inspect ``error``.
    """
    kind = task.kind.value if isinstance(task.kind, TaskKind) else str(task.kind)
    ctx = task.context or TaskContext()
    dump = ctx.model_dump()
    repo = str(dump.get("repo", "") or "")
    diff = str(dump.get("unified_diff", "") or "")
    issue_text = str(dump.get("issue_text", "") or "")
    from_version = str(dump.get("from_version", "") or "")
    to_version = str(dump.get("to_version", "") or "")
    dependency = str(ctx.dependency or "")
    source_version = str(ctx.source_version or "")
    target_version = str(ctx.target_version or "")
    goal = task.goal or ""

    try:
        raw: Any
        if kind == "dependency_upgrade":
            from upgradelens.agent.api import DependencyUpgradeAgent

            raw = DependencyUpgradeAgent(mode=mode).run(
                goal,
                repo=repo or None,
                dependency=dependency or None,
                target_version=target_version or None,
                source_version=source_version or None,
            )
        else:
            gw = gateway or build_gateway(mode)
            if kind == "pr_review":
                from upgradelens.capabilities.pr_review.analyzers import (
                    review_pull_request,
                )

                raw = review_pull_request(
                    repo_root=repo, unified_diff=diff, gateway=gw
                )
            elif kind == "issue_repair":
                from upgradelens.capabilities.issue_repair.analyzers import (
                    repair_issue,
                )

                raw = repair_issue(
                    repo_root=repo, issue_text=issue_text, gateway=gw
                )
            elif kind == "security_review":
                from upgradelens.capabilities.security_review.analyzers import (
                    review_security,
                )

                raw = review_security(
                    repo_root=repo,
                    unified_diff=diff,
                    gateway=gw,
                    dependency=dependency,
                    target_version=target_version or None,
                )
            elif kind == "breaking_change":
                from upgradelens.capabilities.breaking_change.analyzers import (
                    review_breaking_changes,
                )

                raw = review_breaking_changes(
                    repo_root=repo,
                    unified_diff=diff,
                    from_version=from_version or "",
                    to_version=to_version or "",
                    gateway=gw,
                )
            else:
                raise NotImplementedError(f"unsupported capability kind: {kind!r}")

        return _normalize(kind, raw, task)
    except Exception as exc:  # surface, never crash the Workbench
        return CapabilityRunResult(
            capability=kind,
            task=task.model_dump(mode="json"),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            summary=f"capability {kind} failed: {exc}",
        )


def _normalize(kind: str, raw: Any, task: SoftwareTask) -> CapabilityRunResult:
    res = CapabilityRunResult(capability=kind, task=task.model_dump(mode="json"))
    res.raw = _to_jsonable_safe(raw)

    if kind == "dependency_upgrade":
        _norm_dependency_upgrade(raw, res)
    elif kind == "pr_review":
        _norm_pr_review(raw, res)
    elif kind == "issue_repair":
        _norm_issue_repair(raw, res)
    elif kind == "security_review":
        _norm_security_review(raw, res)
    elif kind == "breaking_change":
        _norm_breaking_change(raw, res)

    res.trace = _as_list(res.trace)
    res.summary = _summarize(kind, res)
    return res


def _norm_dependency_upgrade(raw: Any, res: CapabilityRunResult) -> None:
    assessment = getattr(raw, "assessment", None)
    if not assessment:
        return
    risks = list(getattr(assessment, "verified_risks", [])) + list(
        getattr(assessment, "degraded_risks", [])
    )
    res.findings = [
        _to_jsonable_safe(_upgrade_finding_view_to_finding(v)) for v in risks
    ]
    res.degradations = list(getattr(assessment, "degradations", []) or [])
    res.test_results = [
        _to_jsonable_safe(_recommended_test_to_proposal(t))
        for t in getattr(assessment, "recommended_tests", []) or []
    ]
    res.action_proposals = list(res.test_results)
    upgrade_plan = getattr(raw, "upgrade_plan", None)
    patch = getattr(upgrade_plan, "patch", None) if upgrade_plan else None
    if patch is not None:
        res.patch = _to_jsonable_safe(patch)
    res.trace = _to_jsonable_safe(getattr(raw, "trace", []) or [])
    # Synthesize a verification: degraded risks mean the upgrade is not clean.
    degraded = len(getattr(assessment, "degraded_risks", []) or [])
    res.verification = _to_jsonable_safe(
        VerificationResult(
            proposal_id="dependency_upgrade",
            summary=getattr(assessment, "verdict", "") or "assessment",
            checks=[
                VerificationCheck(
                    name="degradations",
                    passed=degraded == 0,
                    detail=f"{degraded} degraded risk(s)",
                )
            ],
        )
    )


def _norm_pr_review(raw: Any, res: CapabilityRunResult) -> None:
    findings = list(getattr(raw, "findings", []) or [])
    gap_findings = list(getattr(raw, "test_gap_findings", []) or [])
    res.findings = [_to_jsonable_safe(f) for f in findings + gap_findings]
    res.test_results = [_to_jsonable_safe(t) for t in (getattr(raw, "tests", []) or [])]
    res.action_proposals = list(res.test_results)
    verification = getattr(raw, "verification", None)
    if verification is not None:
        res.verification = _to_jsonable_safe(verification)
    res.trace = _to_jsonable_safe(getattr(raw, "used", []) or [])
    res.cost = _to_jsonable_safe(getattr(raw, "used", []) or [])


def _norm_issue_repair(raw: Any, res: CapabilityRunResult) -> None:
    res.findings = [_to_jsonable_safe(f) for f in (getattr(raw, "findings", []) or [])]
    actions = [a for a in (getattr(raw, "actions", []) or [])]
    patches = [a for a in actions if isinstance(a, PatchProposal)]
    tests = [_to_jsonable_safe(t) for t in (getattr(raw, "repro_tests", []) or [])]
    res.test_results = tests
    res.action_proposals = [_to_jsonable_safe(a) for a in actions] + tests
    if patches:
        res.patch = _to_jsonable_safe(patches[0])
    verification = getattr(raw, "verification", None)
    if verification is not None:
        res.verification = _to_jsonable_safe(verification)
    res.trace = _to_jsonable_safe(getattr(raw, "used", []) or [])
    res.cost = _to_jsonable_safe(getattr(raw, "used", []) or [])


def _norm_security_review(raw: Any, res: CapabilityRunResult) -> None:
    res.findings = [_to_jsonable_safe(f) for f in (getattr(raw, "findings", []) or [])]
    res.test_results = [
        _to_jsonable_safe(t) for t in (getattr(raw, "test_proposals", []) or [])
    ]
    res.action_proposals = list(res.test_results)
    gate = getattr(raw, "gate", None)
    if gate is not None:
        res.verification = _to_jsonable_safe(gate)
    coverage = getattr(raw, "coverage", None)
    if coverage is not None:
        res.coverage = _to_jsonable_safe(coverage)
    report = getattr(raw, "report", None)
    if report is not None:
        res.security_results = _to_jsonable_safe(report)
    res.cost = {
        "model": getattr(raw, "model_name", ""),
        "used_model": getattr(raw, "used_model", ""),
    }


def _norm_breaking_change(raw: Any, res: CapabilityRunResult) -> None:
    res.findings = [_to_jsonable_safe(f) for f in (getattr(raw, "findings", []) or [])]
    verification = getattr(raw, "verification", None)
    if verification is not None:
        res.verification = _to_jsonable_safe(verification)
    res.trace = _to_jsonable_safe(getattr(raw, "used", []) or [])
    res.cost = _to_jsonable_safe(getattr(raw, "used", []) or [])


def _upgrade_finding_view_to_finding(view: UpgradeFindingView) -> Finding:
    evidence_ids = [
        c.evidence_id for c in getattr(view, "code", []) if c.evidence_id
    ] + [d.evidence_id for d in getattr(view, "docs", []) if d.evidence_id]
    sev = view.severity if view.severity in _SEVERITY_NAMES else "low"
    ev = view.evidence_status
    if ev == "verified" and evidence_ids:
        status = FindingStatus.VERIFIED
        confidence = 0.9
    elif ev == "degraded":
        status = FindingStatus.DEGRADED
        confidence = 0.6
    else:
        status = FindingStatus.CANDIDATE
        confidence = 0.4
    return Finding(
        finding_id=view.risk_id or f"risk-{view.title}",
        category="dependency_risk",
        severity=Severity(sev),
        confidence=confidence,
        summary=view.title or "dependency risk",
        detail=getattr(view, "recommendation", "") or "",
        evidence_ids=evidence_ids,
        status=status,
        requires_approval=status is FindingStatus.VERIFIED,
    )


def _recommended_test_to_proposal(test: dict[str, Any]) -> TestProposal:
    return TestProposal(
        proposal_id=f"test-{test.get('name', 'rec')}",
        kind=ActionKind.TEST,
        title=str(test.get("name", "recommended test")),
        description=str(test.get("rationale", "")),
        test_paths=[str(p) for p in (test.get("target_files") or [])],
        command=str(test.get("test_command", "") or ""),
        intended_to_fail_before_fix=False,
    )


def _summarize(kind: str, res: CapabilityRunResult) -> str:
    fcount = len(res.findings)
    if res.verification and res.verification.get("passed"):
        ver = "verified"
    elif res.verification:
        ver = "not verified"
    else:
        ver = "n/a"
    if res.coverage is not None:
        cov = res.coverage.get("coverage_rate")
        return f"{kind}: {fcount} findings, verification {ver}, coverage {cov}"
    return f"{kind}: {fcount} findings, verification {ver}"
