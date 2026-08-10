"""One assessment pipeline behind every front door.

``assess``, ``comment-pr``, the MCP ``assess`` tool and the Streamlit demo all
answer the same question -- "what breaks if this repository upgrades this
dependency?" -- and each of them used to re-derive the same sequence by hand.
Three copies of a sequence is three chances to drift, and they had already
drifted:

* the CLI deleted a live checkout *before* verifying the report against it, so
  every risk about a cloned repo was checked against a directory that no longer
  existed (and, later, so was the patch draft);
* the demo resolved Skill Packs with a narrower rule than the CLI, reporting
  "no skill" for inputs the CLI matched fine;
* the same two degradations were worded differently in each copy.

This module owns the sequence. The front doors keep only what is genuinely
theirs: argument parsing, rendering, and -- for the demo -- the canned model
responses that make an offline walkthrough interesting.

The steps run through :mod:`upgradelens.tools.registry` rather than importing
the underlying functions directly. That costs a JSON round trip per step and
buys two things: every run leaves a :class:`~upgradelens.tools.trace.ToolTrace`
(which stage A7 will persist as ``trace.jsonl``), and the deterministic pipeline
exercises the exact call surface a future model-driven loop will use, so the two
cannot silently diverge.

Evidence collection and analysis are deliberately two calls rather than one.
The demo needs to look at the finished bundle before it can build the fake
responses the model will "return", and a future agent loop will want to inspect
the evidence before deciding what to do with it. :func:`run_pipeline` composes
the two for callers that need neither.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from upgradelens.db.vector import EmbeddingBackend
from upgradelens.docs.retrieval import retrieve_for_package
from upgradelens.domain import DependencyScanResult, IssueCode, ResolutionStatus
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.domain.skill import SkillPackage
from upgradelens.graph.main import run_assessment
from upgradelens.graph.state import AssessmentSpec
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.models.impact import (
    EvidenceBundle,
    EvidenceItem,
    ImpactReport,
    SourceVersion,
    build_bundle,
)
from upgradelens.skills import builtin_registry
from upgradelens.tools.live_repo import is_repo_url
from upgradelens.tools.registry import ToolContext, ToolRegistry, default_registry
from upgradelens.verify.models import VerifiedReport

# --------------------------------------------------------------------------- #
# Degradations
# --------------------------------------------------------------------------- #
# A degradation is a caveat the *pipeline* knows about its own inputs. The
# verifier turns a non-empty list into ``partial=True``, which caps how loudly
# the report is allowed to speak. Keeping the strings here means all four front
# doors report the same shortfall with the same words.

NO_DOC_INDEX = (
    "No documentation index was provided (--db); "
    "risks cannot reach 'verified' without doc evidence."
)

NO_CODE_EVIDENCE = (
    "No usage of the dependency was found in the code; "
    "the assessment cannot be specific to this repository."
)


# Note: the absence of a Skill Pack is deliberately *not* a degradation. Since
# stage B2/B3 retrieval, planning, scoring and verification all run off shared
# corpus evidence, so a missing pack costs no capability and must not make the
# report speak more quietly. Only genuinely missing evidence degrades a run.


# --------------------------------------------------------------------------- #
# Request / result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AssessmentRequest:
    """What to assess, independent of who asked."""

    repo: str
    dependency: str
    target_version: str | None = None
    #: Optional version the repository is being upgraded *from*. When omitted the
    #: pipeline infers it from the manifest via ``scan_dependency``; when supplied
    #: it is treated as a user-declared from-version (origin="user").
    source_version: str | None = None
    #: SQLite evidence store with ingested docs. Without it, no risk can be
    #: verified -- doc evidence is what promotes a risk beyond "suspected".
    db: Path | None = None
    #: Restrict doc retrieval to a single documentation source id.
    source_id: str | None = None
    #: Branch or tag, only meaningful when ``repo`` is a GitHub URL.
    ref: str | None = None
    #: Free-text description of what the upgrade is for. When empty the
    #: retrieval path falls back to the code symbols discovered by the scan
    #: (stage B2: the main retrieval no longer depends on a dedicated Skill).
    user_intent: str = ""


@dataclass
class EvidenceCollection:
    """Everything the model will see, plus the caveats that came with it.

    Mutable on purpose: a caller may append its own degradation or inject an
    extra evidence item before handing this to :func:`analyse`.
    """

    request: AssessmentRequest
    #: The directory actually analysed -- a temp checkout for live repos, whose
    #: lifetime is owned by the :class:`ToolContext` used to collect.
    repo_path: Path
    spec: AssessmentSpec
    code_report: CodeEvidenceReport
    skill: SkillPackage | None
    bundle: EvidenceBundle
    degradations: list[str] = field(default_factory=list)
    #: Resolved from-version (declared/inferred from the manifest, or user-supplied).
    source_version: SourceVersion | None = None
    #: Raw ``scan_dependency`` result, retained for trace/debug and reporting.
    dependency_scan: DependencyScanResult | None = None


@dataclass(frozen=True)
class AssessmentOutcome:
    """A verified assessment, plus the inputs a caller may still need.

    ``bundle`` and ``code_report`` are carried through because patch drafting
    and PR rendering need them, and re-deriving them would mean scanning twice.
    """

    report: ImpactReport
    verified: VerifiedReport
    repo_path: Path
    skill: SkillPackage | None
    bundle: EvidenceBundle
    code_report: CodeEvidenceReport
    degradations: tuple[str, ...]
    #: Resolved from-version (declared/inferred from the manifest, or user-supplied).
    source_version: SourceVersion | None = None
    #: Raw ``scan_dependency`` result, retained for trace/debug and reporting.
    dependency_scan: DependencyScanResult | None = None


def _resolve_source_version(
    request: AssessmentRequest, scan_result: DependencyScanResult | None
) -> SourceVersion:
    """Turn the optional user override and the manifest scan into a SourceVersion."""
    if request.source_version is not None:
        return SourceVersion(spec=request.source_version, origin="user", status="declared")
    if scan_result is None:
        return SourceVersion(spec=None, origin="declared", status="unknown")
    if any(
        issue.code == IssueCode.CONFLICTING_DECLARATIONS
        for issue in (*scan_result.warnings, *scan_result.errors)
    ):
        return SourceVersion(spec=None, origin="declared", status="conflict")
    if scan_result.status == ResolutionStatus.RESOLVED:
        spec = scan_result.current_specifier or scan_result.current_version or None
        return SourceVersion(spec=spec, origin="declared", status="declared")
    if scan_result.status == ResolutionStatus.AMBIGUOUS:
        return SourceVersion(
            spec=scan_result.current_specifier or None,
            origin="declared",
            status="inferred",
        )
    return SourceVersion(spec=None, origin="declared", status="unknown")


def _add_declaration_evidence(
    bundle: EvidenceBundle,
    request: AssessmentRequest,
    source_version: SourceVersion,
    scan_result: DependencyScanResult | None,
) -> None:
    """Record the resolved from-version as a citable evidence item."""
    if scan_result is not None and scan_result.declarations:
        decl = scan_result.declarations[0]
        summary = f"Declared as {decl.specifier} in {decl.manifest_type} ({decl.path})"
        detail = scan_result.model_dump_json()
    elif request.source_version:
        summary = f"User-provided source version: {request.source_version}"
        detail = ""
    else:
        summary = "No declared source version found in any manifest."
        detail = ""
    bundle.add(
        EvidenceItem(
            evidence_id="dep_decl",
            kind="dependency_declaration",
            summary=summary,
            detail=detail,
            meta={
                "dependency": request.dependency,
                "source_version_spec": source_version.spec or "",
                "source_version_origin": source_version.origin,
                "source_version_status": source_version.status,
            },
        )
    )


# --------------------------------------------------------------------------- #
# Phase 1: collect
# --------------------------------------------------------------------------- #


def collect_evidence(
    request: AssessmentRequest,
    ctx: ToolContext,
    *,
    registry: ToolRegistry | None = None,
    gateway: ModelGateway | None = None,
    mode: ModelMode | str = ModelMode.FAKE,
    embedding: EmbeddingBackend | None = None,
) -> EvidenceCollection:
    """Gather every citable fact about ``request``, without asking a model.

    ``ctx`` owns any temporary checkout and database session, so it must stay
    open until the caller is finished with :attr:`EvidenceCollection.repo_path`
    -- the verifier and the patch drafter both read files from it.

    Raises:
        ToolError: a live repository could not be cloned, or a scan failed.
    """
    tools = registry or default_registry()

    repo_path = _checkout(request, ctx, tools)
    code_report = CodeEvidenceReport.model_validate(
        tools.run("scan_code", {"repo": str(repo_path), "dependency": request.dependency}, ctx)
    )
    skill = _resolve_skill(request, ctx, tools)

    target_version = _target_spec(request, skill)
    scan_result: DependencyScanResult | None = None
    if target_version:
        try:
            scan_result = DependencyScanResult.model_validate(
                tools.run(
                    "scan_dependency",
                    {
                        "repo": str(repo_path),
                        "dependency": request.dependency,
                        "target_version": target_version,
                    },
                    ctx,
                )
            )
        except Exception:
            scan_result = None
    source_version = _resolve_source_version(request, scan_result)

    degradations: list[str] = []
    if request.db is None:
        degradations.append(NO_DOC_INDEX)

    session = ctx.session(request.db) if request.db is not None else None
    # Shared-corpus retrieval (stage B2): the main path no longer depends on a
    # dedicated Skill Pack. A skill, when present, only contributes curated
    # boost queries -- it can never block doc evidence from being collected.
    doc_runs: list[RetrievalRun] = []
    if session is not None:
        doc_runs = retrieve_for_package(
            session,
            package=request.dependency,
            source_version=source_version.spec or "",
            target_version=_target_spec(request, skill),
            user_intent=request.user_intent,
            code_symbols=sorted({usage.symbol for usage in code_report.usages}),
            source_id=request.source_id,
            curated_queries=(
                [query for pattern in skill.patterns for query in pattern.retrieval_queries]
                if skill is not None
                else []
            ),
            gateway=gateway,
            mode=mode,
            embedding=embedding,
        )

    bundle = build_bundle(code_report, doc_runs, dependency=request.dependency)
    _add_declaration_evidence(bundle, request, source_version, scan_result)
    if source_version.status in ("unknown", "conflict"):
        degradations.append(
            "Source version could not be determined from the manifests "
            f"(status={source_version.status}); the assessment is not anchored "
            "to a specific from-version."
        )
    if not bundle.items:
        degradations.append(NO_CODE_EVIDENCE)

    return EvidenceCollection(
        request=request,
        repo_path=repo_path,
        spec=AssessmentSpec(
            repo=str(repo_path),
            dependency=request.dependency,
            target_version_spec=_target_spec(request, skill),
            source_version_spec=source_version.spec or "",
            source_version=source_version,
        ),
        code_report=code_report,
        skill=skill,
        bundle=bundle,
        degradations=degradations,
        source_version=source_version,
        dependency_scan=scan_result,
    )


def _checkout(request: AssessmentRequest, ctx: ToolContext, tools: ToolRegistry) -> Path:
    """Resolve ``request.repo`` to a local directory, cloning it if it is a URL."""
    if not is_repo_url(request.repo):
        return Path(request.repo)
    result = tools.run("clone_repo", {"url": request.repo, "ref": request.ref}, ctx)
    return Path(str(result["path"]))


def _resolve_skill(
    request: AssessmentRequest, ctx: ToolContext, tools: ToolRegistry
) -> SkillPackage | None:
    """Pick the Skill Pack for this request.

    Goes through the tool (so the choice shows up in the trace) and then loads
    the package itself: the tool's JSON answer is a summary, while the pipeline
    needs the patterns, sources and patch rules behind it.
    """
    result = tools.run(
        "resolve_skill",
        {"dependency": request.dependency, "target_version": request.target_version},
        ctx,
    )
    skill_id = result.get("skill_id")
    if not skill_id:
        return None
    return builtin_registry().get(str(skill_id))


def _target_spec(request: AssessmentRequest, skill: SkillPackage | None) -> str:
    """Caller's target version, else the skill's, else nothing."""
    if request.target_version:
        return request.target_version
    if skill is not None:
        return skill.target_version_spec or ""
    return ""


# --------------------------------------------------------------------------- #
# Phase 2: analyse
# --------------------------------------------------------------------------- #


def analyse(
    collection: EvidenceCollection,
    gateway: ModelGateway,
    ctx: ToolContext,
    *,
    registry: ToolRegistry | None = None,
) -> AssessmentOutcome:
    """Run the closed loop over collected evidence and verify the result.

    Verification happens while ``ctx`` is still open, because it re-reads the
    repository to confirm that every cited location is real.
    """
    tools = registry or default_registry()
    report = run_assessment(collection.spec, collection.bundle, gateway, skill=collection.skill)
    verified = VerifiedReport.model_validate(
        tools.run(
            "verify_report",
            {
                "repo": str(collection.repo_path),
                "report": report.model_dump(mode="json"),
                "bundle": [asdict(item) for item in collection.bundle.items],
                "code_report": collection.code_report.model_dump(mode="json"),
                "skill_id": collection.skill.skill_id if collection.skill else None,
                "degradations": list(collection.degradations),
            },
            ctx,
        )
    )
    return AssessmentOutcome(
        report=report,
        verified=verified,
        repo_path=collection.repo_path,
        skill=collection.skill,
        bundle=collection.bundle,
        code_report=collection.code_report,
        degradations=tuple(collection.degradations),
        source_version=collection.source_version,
        dependency_scan=collection.dependency_scan,
    )


def run_pipeline(
    request: AssessmentRequest,
    gateway: ModelGateway,
    ctx: ToolContext,
    *,
    registry: ToolRegistry | None = None,
    embedding: EmbeddingBackend | None = None,
) -> AssessmentOutcome:
    """Collect, analyse and verify in one call.

    For callers that do not need to touch the evidence in between. ``ctx`` stays
    the caller's responsibility because the returned
    :attr:`AssessmentOutcome.repo_path` may point into a temporary checkout.
    """
    tools = registry or default_registry()
    collection = collect_evidence(
        request,
        ctx,
        registry=tools,
        gateway=gateway,
        mode=gateway.mode if gateway is not None else ModelMode.FAKE,
        embedding=embedding,
    )
    return analyse(collection, gateway, ctx, registry=tools)


__all__ = [
    "NO_CODE_EVIDENCE",
    "NO_DOC_INDEX",
    "AssessmentOutcome",
    "AssessmentRequest",
    "EvidenceCollection",
    "analyse",
    "collect_evidence",
    "run_pipeline",
]
