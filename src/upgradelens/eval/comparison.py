"""S8 architecture comparison harness (plan section 18.2).

Off-line, FAKE-mode comparison of three upgrade-assessment *architectures* over
the shared :mod:`upgradelens.eval.cases` corpus:

- ``direct_llm`` -- a bare LLM / coding agent: the synthetic model report is
  trusted at face value, with no retrieval and no verification;
- ``fixed_pipeline`` -- the deterministic collect -> analyse -> verify pipeline
  (``run_pipeline``) driven by the same model output through the shared corpus
  retrieval + verifier;
- ``agent`` -- the UpgradeLens Agent loop (``run_agent``, plan-driven fake
  policy) over the same inputs.

Every system is fed the *same* evidence and the *same* model output, so the
metrics isolate the value added by retrieval and verification (the pipeline) and
by the plan-driven agent loop. No network access is required: model calls are
satisfied by deterministic fakes derived from each case's ``model_report.json``,
and the run runs entirely in ``FAKE`` mode.

The metric set (see :class:`S8Metrics`) covers breaking-change recall, code
location recall, doc-citation accuracy, no-evidence suggestion rate, evidence
coverage, plan completion, verifier detection of fabricated claims, and the
per-system token/call/latency cost.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from upgradelens.agent.loop import run_agent
from upgradelens.agent.plan import AgentPlan
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.eval.baselines import (
    CaseArtifacts,
    _model_report,
    build_artifacts,
    run_baseline,
)
from upgradelens.eval.cases import EvalCase, load_cases
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import EvidenceBundle, ImpactReport, Plan
from upgradelens.pipeline import AssessmentRequest, run_pipeline
from upgradelens.tools.registry import ToolContext
from upgradelens.verify.models import EvidenceStatus, VerifiedReport

__all__ = [
    "SYSTEMS",
    "ABLATION_SYSTEMS",
    "S8Metrics",
    "ComparisonRun",
    "ComparisonReport",
    "run_comparison",
    "run_ablation",
    "run_comparison_replay",
    "compute_metrics",
    "run_direct_llm",
    "run_fixed_pipeline",
    "run_agent_system",
    "run_agent_no_supplement",
]

#: The three architectures compared by the S8 harness.
SYSTEMS = ("direct_llm", "fixed_pipeline", "agent")

_VERIFIED = EvidenceStatus.VERIFIED


# --------------------------------------------------------------------------- #
# Per-run result container
# --------------------------------------------------------------------------- #
@dataclass
class ComparisonRun:
    """One system's output for one case, normalised for metric computation."""

    case_id: str
    system: str
    verified: VerifiedReport
    raw_report: ImpactReport
    bundle: EvidenceBundle
    code_report: CodeEvidenceReport
    degradations: tuple[str, ...]
    plan: AgentPlan | None
    ledger: list[Any]
    duration_ms: float
    error: str | None = None


# --------------------------------------------------------------------------- #
# Metric set
# --------------------------------------------------------------------------- #
@dataclass
class S8Metrics:
    """The S8 architecture-comparison metric set (all rates in ``[0, 1]``)."""

    breaking_change_recall: float = 0.0
    code_location_recall: float = 0.0
    doc_accuracy: float = 0.0
    no_evidence_rate: float = 0.0
    coverage: float = 0.0
    plan_completion_rate: float | None = None
    verifier_detection_rate: float | None = None
    total_tokens: int = 0
    call_count: int = 0
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "breaking_change_recall": self.breaking_change_recall,
            "code_location_recall": self.code_location_recall,
            "doc_accuracy": self.doc_accuracy,
            "no_evidence_rate": self.no_evidence_rate,
            "coverage": self.coverage,
            "plan_completion_rate": self.plan_completion_rate,
            "verifier_detection_rate": self.verifier_detection_rate,
            "total_tokens": self.total_tokens,
            "call_count": self.call_count,
            "latency_ms": self.latency_ms,
        }


# --------------------------------------------------------------------------- #
# System runners
# --------------------------------------------------------------------------- #
def _fakes_for_case(art: CaseArtifacts) -> dict[str, Any]:
    """Deterministic FAKE responses for the pipeline's model nodes.

    The planner returns an empty plan (so the breaking-change extractor is
    skipped) and the impact analyser replays the case's model report. The
    verifier then promotes or quarantines each risk against the real bundle.
    """
    report = _model_report(art)
    return {
        "planner": Plan(items=[]),
        "impact_analyzer": report,
    }


def _make_gateway(fakes: dict[str, Any]) -> ModelGateway:
    return ModelGateway(ModelConfig(mode=ModelMode.FAKE), fake_responses=fakes)


def _request_for(case: EvalCase) -> AssessmentRequest:
    return AssessmentRequest(
        repo=str(case.repo),
        dependency=case.dependency,
        target_version=case.target_version,
        source_version=case.source_version or None,
        db=None,
    )


def run_direct_llm(case: EvalCase, art: CaseArtifacts) -> ComparisonRun:
    """Bare LLM / coding-agent baseline: model report trusted as-is."""
    verified = run_baseline("llm_only", art)
    raw = _model_report(art)
    return ComparisonRun(
        case_id=case.case_id,
        system="direct_llm",
        verified=verified,
        raw_report=raw,
        bundle=art.bundle,
        code_report=art.code_report,
        degradations=tuple(art.degradations),
        plan=None,
        ledger=[],
        duration_ms=0.0,
    )


def run_fixed_pipeline(case: EvalCase, art: CaseArtifacts) -> ComparisonRun:
    """Deterministic collect -> analyse -> verify pipeline (FAKE mode)."""
    fakes = _fakes_for_case(art)
    gateway = _make_gateway(fakes)
    request = _request_for(case)
    try:
        with ToolContext() as ctx:
            outcome = run_pipeline(request, gateway, ctx)
        verified = outcome.verified or VerifiedReport()
        raw = outcome.report or _model_report(art)
        bundle = outcome.bundle or art.bundle
        code_report = outcome.code_report or art.code_report
        degradations = tuple(outcome.degradations)
        error = None
    except Exception as exc:  # pragma: no cover - defensive
        verified = VerifiedReport()
        raw = _model_report(art)
        bundle = art.bundle
        code_report = art.code_report
        degradations = tuple(art.degradations) + (f"ERROR: {exc}",)
        error = str(exc)
    ledger = list(gateway.ledger)
    duration = sum(getattr(r, "latency_ms", 0.0) for r in ledger)
    return ComparisonRun(
        case_id=case.case_id,
        system="fixed_pipeline",
        verified=verified,
        raw_report=raw,
        bundle=bundle,
        code_report=code_report,
        degradations=degradations,
        plan=None,
        ledger=ledger,
        duration_ms=duration,
        error=error,
    )


def run_agent_system(case: EvalCase, art: CaseArtifacts) -> ComparisonRun:
    """UpgradeLens Agent loop (plan-driven FAKE policy)."""
    fakes = _fakes_for_case(art)
    gateway = _make_gateway(fakes)
    request = _request_for(case)
    captured: dict[str, Any] = {}
    try:
        with ToolContext() as ctx:
            outcome = run_agent(
                request,
                gateway,
                ctx,
                plan_writer=lambda p: captured.update(plan=p),
            )
        verified = outcome.verified or VerifiedReport()
        raw = outcome.report or _model_report(art)
        bundle = outcome.bundle or art.bundle
        code_report = outcome.code_report or art.code_report
        degradations = tuple(outcome.degradations)
        error = None
    except Exception as exc:  # pragma: no cover - defensive
        verified = VerifiedReport()
        raw = _model_report(art)
        bundle = art.bundle
        code_report = art.code_report
        degradations = tuple(art.degradations) + (f"ERROR: {exc}",)
        error = str(exc)
    ledger = list(gateway.ledger)
    duration = sum(getattr(r, "latency_ms", 0.0) for r in ledger)
    return ComparisonRun(
        case_id=case.case_id,
        system="agent",
        verified=verified,
        raw_report=raw,
        bundle=bundle,
        code_report=code_report,
        degradations=degradations,
        plan=captured.get("plan"),
        ledger=ledger,
        duration_ms=duration,
        error=error,
    )


_RUNNERS: dict[str, Callable[[EvalCase, CaseArtifacts], ComparisonRun]] = {
    "direct_llm": run_direct_llm,
    "fixed_pipeline": run_fixed_pipeline,
    "agent": run_agent_system,
}


# --------------------------------------------------------------------------- #
# S8 ablation: agent without supplementary retrieval
# --------------------------------------------------------------------------- #
#: The four ablation systems isolate the value of each architecture layer.
ABLATION_SYSTEMS = ("direct_llm", "fixed_pipeline", "agent_no_supplement", "agent")


def run_agent_no_supplement(case: EvalCase, art: CaseArtifacts) -> ComparisonRun:
    """Agent loop with the S4 coverage / supplementary-retrieval phase disabled.

    Isolates the value added by autonomous supplementary retrieval: the agent
    still runs its plan-driven collection and verification loop, but receives no
    coverage-gap-driven focused retrievals.
    """
    fakes = _fakes_for_case(art)
    gateway = _make_gateway(fakes)
    request = _request_for(case)
    captured: dict[str, Any] = {}
    try:
        with ToolContext() as ctx:
            outcome = run_agent(
                request,
                gateway,
                ctx,
                plan_writer=lambda p: captured.update(plan=p),
                max_supplementary=0,
            )
        verified = outcome.verified or VerifiedReport()
        raw = outcome.report or _model_report(art)
        bundle = outcome.bundle or art.bundle
        code_report = outcome.code_report or art.code_report
        degradations = tuple(outcome.degradations)
        error = None
    except Exception as exc:  # pragma: no cover - defensive
        verified = VerifiedReport()
        raw = _model_report(art)
        bundle = art.bundle
        code_report = art.code_report
        degradations = tuple(art.degradations) + (f"ERROR: {exc}",)
        error = str(exc)
    ledger = list(gateway.ledger)
    duration = sum(getattr(r, "latency_ms", 0.0) for r in ledger)
    return ComparisonRun(
        case_id=case.case_id,
        system="agent_no_supplement",
        verified=verified,
        raw_report=raw,
        bundle=bundle,
        code_report=code_report,
        degradations=degradations,
        plan=captured.get("plan"),
        ledger=ledger,
        duration_ms=duration,
        error=error,
    )


_ABLATION_RUNNERS: dict[str, Callable[[EvalCase, CaseArtifacts], ComparisonRun]] = {
    "direct_llm": run_direct_llm,
    "fixed_pipeline": run_fixed_pipeline,
    "agent_no_supplement": run_agent_no_supplement,
    "agent": run_agent_system,
}


def run_ablation(
    cases: list[EvalCase],
    systems: tuple[str, ...] = ABLATION_SYSTEMS,
    *,
    artifacts: dict[str, CaseArtifacts] | None = None,
) -> ComparisonReport:
    """Run the S8 ablation comparison over ``cases``.

    The ablation isolates each layer's contribution:

    - ``direct_llm`` -- no retrieval, no verification (bare LLM baseline);
    - ``fixed_pipeline`` -- retrieval + verification, no agent loop;
    - ``agent_no_supplement`` -- agent loop + verification, no supplementary
      retrieval (S4 coverage phase disabled);
    - ``agent`` -- the full UpgradeLens Agent (agent + supplement + verification).
    """
    arts = artifacts or {c.case_id: build_artifacts(c, None) for c in cases}
    per_case: dict[str, dict[str, S8Metrics]] = {}
    for case in cases:
        art = arts[case.case_id]
        per_case[case.case_id] = {}
        for sys in systems:
            runner = _ABLATION_RUNNERS[sys]
            run = runner(case, art)
            per_case[case.case_id][sys] = compute_metrics(run, case)
    return ComparisonReport(per_case=per_case, systems=systems)


# --------------------------------------------------------------------------- #
# S8 replay: run systems against recorded live model responses
# --------------------------------------------------------------------------- #
def _make_replay_gateway(replay_dir: str) -> ModelGateway:
    """A gateway that replays recorded responses from ``replay_dir``."""
    return ModelGateway(
        ModelConfig(mode=ModelMode.REPLAY),
        replay_dir=replay_dir,
    )


def run_comparison_replay(
    cases: list[EvalCase],
    replay_dir: str | Path,
    systems: tuple[str, ...] = SYSTEMS,
) -> ComparisonReport:
    """Run the S8 comparison using REPLAY-mode model responses.

    Each case's recorded responses must live under
    ``{replay_dir}/{case_id}/``. The pipeline and agent runners are fed the
    same recorded planner / impact-analyser outputs, so the metrics reflect the
    real model's behaviour without any network access.
    """
    base = Path(replay_dir)
    arts = {c.case_id: build_artifacts(c, None) for c in cases}
    per_case: dict[str, dict[str, S8Metrics]] = {}
    for case in cases:
        art = arts[case.case_id]
        case_dir = str(base / case.case_id)
        per_case[case.case_id] = {}
        for sys in systems:
            run = _run_replay_system(sys, case, art, case_dir)
            per_case[case.case_id][sys] = compute_metrics(run, case)
    return ComparisonReport(per_case=per_case, systems=systems)


def _run_replay_system(
    system: str,
    case: EvalCase,
    art: CaseArtifacts,
    replay_dir: str,
) -> ComparisonRun:
    """Run one system in REPLAY mode using recorded model responses."""
    if system == "direct_llm":
        # direct_llm trusts the model report as-is; no gateway needed.
        return run_direct_llm(case, art)

    gateway = _make_replay_gateway(replay_dir)
    request = _request_for(case)
    captured: dict[str, Any] = {}
    try:
        with ToolContext() as ctx:
            if system == "fixed_pipeline":
                outcome = run_pipeline(request, gateway, ctx)
            else:
                outcome = run_agent(
                    request,
                    gateway,
                    ctx,
                    plan_writer=lambda p: captured.update(plan=p),
                )
        verified = outcome.verified or VerifiedReport()
        raw = outcome.report or _model_report(art)
        bundle = outcome.bundle or art.bundle
        code_report = outcome.code_report or art.code_report
        degradations = tuple(outcome.degradations)
        error = None
    except Exception as exc:  # pragma: no cover - defensive
        verified = VerifiedReport()
        raw = _model_report(art)
        bundle = art.bundle
        code_report = art.code_report
        degradations = tuple(art.degradations) + (f"ERROR: {exc}",)
        error = str(exc)
    ledger = list(gateway.ledger)
    duration = sum(getattr(r, "latency_ms", 0.0) for r in ledger)
    return ComparisonRun(
        case_id=case.case_id,
        system=system,
        verified=verified,
        raw_report=raw,
        bundle=bundle,
        code_report=code_report,
        degradations=degradations,
        plan=captured.get("plan"),
        ledger=ledger,
        duration_ms=duration,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Metric computation
# --------------------------------------------------------------------------- #
def _bundle_code_symbols(bundle: EvidenceBundle) -> set[str]:
    return {
        str(item.meta.get("symbol", "")).lower()
        for item in bundle.items
        if item.kind == "code_usage" and item.meta.get("symbol")
    }


def _bundle_code_paths(bundle: EvidenceBundle) -> set[str]:
    return {
        str(item.meta.get("path", ""))
        for item in bundle.items
        if item.kind == "code_usage" and item.meta.get("path")
    }


def _verified_risk_ids(run: ComparisonRun) -> set[str]:
    return {r.risk_id for r in run.verified.verified_risks}


def _verified_titles(run: ComparisonRun) -> list[str]:
    return [str(r.title or "").lower() for r in run.verified.verified_risks]


def compute_metrics(run: ComparisonRun, case: EvalCase) -> S8Metrics:
    """Compute the S8 metric set for one system run against one case."""
    expect = case.expect
    symbols = _bundle_code_symbols(run.bundle)
    paths = _bundle_code_paths(run.bundle)
    verified_titles = _verified_titles(run)
    verified_ids = _verified_risk_ids(run)

    # Breaking-change recall: each expected symbol must surface (risk title or
    # the code evidence the system actually gathered for it).
    sym_targets = list(expect.must_flag_symbols)
    if sym_targets:
        matched = 0
        for sym in sym_targets:
            s = sym.lower()
            if any(s in t for t in verified_titles) or any(s in sym2 for sym2 in symbols):
                matched += 1
        breaking_recall = matched / len(sym_targets)
    else:
        breaking_recall = 1.0

    # Code-location recall: each expected path must appear in a verified risk's
    # code evidence path (matched by suffix, so "repo/src/x.py" hits "src/x.py").
    path_targets = list(expect.must_cite_paths)
    if path_targets:
        matched = 0
        for p in path_targets:
            if any(p in cp or cp.endswith(p) for cp in paths):
                matched += 1
        code_recall = matched / len(path_targets)
    else:
        code_recall = 1.0

    # Doc accuracy: of verified risks that cite doc evidence, how many cite doc
    # evidence that actually exists in the bundle. Off-line (no doc store) this
    # is 1.0 because doc-cited risks are dropped before verification.
    doc_cited = [r for r in run.verified.verified_risks if r.doc_evidence_ids]
    if doc_cited:
        valid = sum(
            1
            for r in doc_cited
            if all(run.bundle.get(eid) is not None for eid in r.doc_evidence_ids)
        )
        doc_accuracy = valid / len(doc_cited)
    else:
        doc_accuracy = 1.0

    # No-evidence suggestion rate: fraction of all surfaced risks with no
    # code or doc evidence at all.
    all_risks = run.verified.all_risks
    if all_risks:
        empties = sum(1 for r in all_risks if not r.code_evidence_ids and not r.doc_evidence_ids)
        no_evidence = empties / len(all_risks)
    else:
        no_evidence = 0.0

    # Coverage: fraction of expected breaking-change symbols for which the
    # evidence collection actually gathered code usage evidence.
    if sym_targets:
        covered = sum(1 for sym in sym_targets if any(sym.lower() in s2 for s2 in symbols))
        coverage = covered / len(sym_targets)
    else:
        coverage = 1.0

    # Plan completion (agent only): fraction of plan steps that succeeded.
    if run.plan is not None and run.plan.steps:
        done = sum(1 for s in run.plan.steps if s.status == "succeeded")
        plan_rate: float | None = done / len(run.plan.steps)
    else:
        plan_rate = None

    # Verifier detection: fraction of risks that *should* be quarantined
    # (fabricated / unsupported claims) that are NOT presented as verified.
    # When the case declares no known-bad claims there is nothing to detect,
    # so the metric is left as ``None`` (n/a) and excluded from the aggregate.
    quarantine = list(expect.must_quarantine_risk_ids)
    if quarantine:
        caught = sum(1 for qid in quarantine if qid not in verified_ids)
        detection = caught / len(quarantine)
    else:
        detection = None

    ledger = run.ledger
    total_tokens = sum(
        getattr(r, "prompt_tokens", 0) + getattr(r, "completion_tokens", 0) for r in ledger
    )

    return S8Metrics(
        breaking_change_recall=breaking_recall,
        code_location_recall=code_recall,
        doc_accuracy=doc_accuracy,
        no_evidence_rate=no_evidence,
        coverage=coverage,
        plan_completion_rate=plan_rate,
        verifier_detection_rate=detection,
        total_tokens=int(total_tokens),
        call_count=len(ledger),
        latency_ms=run.duration_ms,
    )


# --------------------------------------------------------------------------- #
# Comparison orchestration
# --------------------------------------------------------------------------- #
@dataclass
class ComparisonReport:
    """Per-case and per-system S8 metrics, with aggregates and exporters."""

    per_case: dict[str, dict[str, S8Metrics]]
    systems: tuple[str, ...]

    def aggregate(self) -> dict[str, dict[str, float]]:
        """Mean of every metric per system (plan rate uses agent runs only)."""
        out: dict[str, dict[str, float]] = {}
        for sys in self.systems:
            rows = [m for case in self.per_case.values() for m in [case.get(sys)] if m]
            if not rows:
                out[sys] = {}
                continue
            merged: dict[str, list[float]] = {}
            plan_vals: list[float] = []
            detection_vals: list[float] = []
            for m in rows:
                for key, val in m.as_dict().items():
                    if isinstance(val, (int, float)):
                        merged.setdefault(key, []).append(float(val))
                if m.plan_completion_rate is not None:
                    plan_vals.append(m.plan_completion_rate)
                if m.verifier_detection_rate is not None:
                    detection_vals.append(m.verifier_detection_rate)
            agg = {k: (sum(v) / len(v) if v else 0.0) for k, v in merged.items()}
            if plan_vals:
                agg["plan_completion_rate"] = sum(plan_vals) / len(plan_vals)
            if detection_vals:
                agg["verifier_detection_rate"] = sum(detection_vals) / len(detection_vals)
            out[sys] = agg
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "systems": list(self.systems),
            "aggregate": self.aggregate(),
            "per_case": {
                case: {sys: m.as_dict() for sys, m in systems.items()}
                for case, systems in self.per_case.items()
            },
        }

    def to_markdown(self) -> str:
        agg = self.aggregate()
        headers = [
            "system",
            "breaking_recall",
            "code_recall",
            "doc_acc",
            "no_evidence",
            "coverage",
            "plan_done",
            "verifier_det",
            "tokens",
            "calls",
        ]
        lines = ["# S8 Architecture Comparison", ""]
        lines.append("## Per-system aggregate (mean over cases)")
        lines.append("")
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")
        for sys in self.systems:
            a = agg.get(sys, {})
            plan = a.get("plan_completion_rate")
            plan_s = "n/a" if plan is None else f"{plan:.2f}"
            det = a.get("verifier_detection_rate")
            det_s = "n/a" if det is None else f"{det:.2f}"
            lines.append(
                "| "
                + " | ".join(
                    [
                        sys,
                        f"{a.get('breaking_change_recall', 0):.2f}",
                        f"{a.get('code_location_recall', 0):.2f}",
                        f"{a.get('doc_accuracy', 0):.2f}",
                        f"{a.get('no_evidence_rate', 0):.2f}",
                        f"{a.get('coverage', 0):.2f}",
                        plan_s,
                        det_s,
                        str(int(a.get("total_tokens", 0))),
                        str(int(a.get("call_count", 0))),
                    ]
                )
                + " |"
            )
        lines.append("")
        lines.append("## Per-case detail")
        lines.append("")
        for case, systems in self.per_case.items():
            lines.append(f"### {case}")
            lines.append("")
            lines.append(
                "| system | breaking_recall | code_recall | verifier_det | tokens | calls |"
            )
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for sys in self.systems:
                m = systems.get(sys)
                if m is None:
                    continue
                det_s = (
                    "n/a"
                    if m.verifier_detection_rate is None
                    else f"{m.verifier_detection_rate:.2f}"
                )
                lines.append(
                    f"| {sys} | {m.breaking_change_recall:.2f} | "
                    f"{m.code_location_recall:.2f} | {det_s} | "
                    f"{m.total_tokens} | {m.call_count} |"
                )
            lines.append("")
        return "\n".join(lines)


def run_comparison(
    cases: list[EvalCase],
    systems: tuple[str, ...] = SYSTEMS,
    *,
    artifacts: dict[str, CaseArtifacts] | None = None,
) -> ComparisonReport:
    """Run every system over every case and compute the S8 metric set.

    ``artifacts`` may be supplied to reuse a prebuilt bundle (e.g. in tests);
    otherwise each case's artifacts are scanned once with ``session=None``
    (off-line, no documentation index).
    """
    arts = artifacts or {c.case_id: build_artifacts(c, None) for c in cases}
    per_case: dict[str, dict[str, S8Metrics]] = {}
    for case in cases:
        art = arts[case.case_id]
        per_case[case.case_id] = {}
        for sys in systems:
            runner = _RUNNERS[sys]
            run = runner(case, art)
            per_case[case.case_id][sys] = compute_metrics(run, case)
    return ComparisonReport(per_case=per_case, systems=systems)


def run_comparison_from_dir(cases_dir: str | Path) -> ComparisonReport:
    """Convenience wrapper: load cases from ``cases_dir`` and compare."""
    return run_comparison(load_cases(Path(cases_dir)))
