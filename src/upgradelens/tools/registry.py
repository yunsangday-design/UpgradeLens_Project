"""A uniform Tool abstraction over UpgradeLens' capabilities.

Until now every capability (clone a repo, scan code, retrieve docs, verify a
report) was reachable only by importing the right function and knowing its
bespoke signature. The CLI, the MCP server and the graph each re-derived that
knowledge. This module gives all of them -- and, later, a model-driven agent
loop -- one shape:

    tool = default_registry().get("scan_code")
    result = tool.run({"repo": "...", "dependency": "pydantic"})

Every tool declares a pydantic input model, so :meth:`Tool.json_schema` yields
an OpenAI/MCP-compatible function definition for free, and every tool returns a
JSON-safe ``dict``. Failures are normalised to :class:`ToolError` subclasses so
a caller never has to catch an open-ended ``Exception``.

Nothing here performs new outbound IO: the network-touching pieces still go
through the stage 7 fetcher/clone helpers with their SSRF guard intact.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from upgradelens.analyzers import scan_code_evidence
from upgradelens.analyzers import scan_dependency as _scan_dependency
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.db.vector import EmbeddingBackend
from upgradelens.docs import retrieve as _retrieve
from upgradelens.docs.retrieval import retrieve_for_package as _retrieve_for_package
from upgradelens.domain import DependencyAnalysisRequest
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.domain.skill import SkillPackage
from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport
from upgradelens.skills import SkillParseError, builtin_registry
from upgradelens.tools.errors import ToolError, ToolExecutionError, ToolInputError
from upgradelens.tools.live_repo import LiveRepoHandle, clone_live_repo, parse_repo_slug
from upgradelens.tools.trace import ToolTrace
from upgradelens.verify import verify_report as _verify_report
from upgradelens.verify.version_match import extract_version

# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


@dataclass
class ToolContext:
    """Per-run side channel shared by tool invocations.

    It owns the resources a single tool call cannot own by itself: temporary
    clones that must outlive the ``clone_repo`` call (later tools need the
    path) and database sessions worth reusing across retrievals.

    The ``trace`` here is deliberately a *separate* :class:`ToolTrace` from the
    fetcher's: local tool calls are not network calls, and mixing them would
    corrupt :meth:`ToolTrace.network_calls`.
    """

    trace: ToolTrace = field(default_factory=ToolTrace)
    workdir: Path | None = None
    gateway: ModelGateway | None = None
    embedding: EmbeddingBackend | None = None
    # --- plan linkage (ROADMAP Step 3) ----------------------------------- #
    # Set by the agent loop before each tool run so the recorded event carries
    # the owning plan step id and attempt counter.
    active_plan_step_id: str = ""
    active_attempt: int = 0
    _clones: list[LiveRepoHandle] = field(default_factory=list, repr=False)
    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)

    def track_clone(self, handle: LiveRepoHandle) -> LiveRepoHandle:
        """Register a clone so :meth:`close` removes its temp dir."""
        self._clones.append(handle)
        return handle

    def session(self, db: Path) -> Session:
        """Return a cached session for ``db``, initialising the schema once."""
        key = str(Path(db).resolve())
        existing = self._sessions.get(key)
        if existing is not None:
            return existing
        engine = engine_for(Path(db))
        init_db(engine)
        session = session_for(engine)()
        self._sessions[key] = session
        return session

    def close(self) -> None:
        """Release every resource acquired through this context."""
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
        for handle in self._clones:
            handle.cleanup()
        self._clones.clear()

    def __enter__(self) -> ToolContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #

Handler = Callable[[Any, ToolContext], Any]


@dataclass(frozen=True)
class Tool:
    """One named capability with a validated input model and a JSON result."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    #: True when the tool may reach the network (always through the guarded
    #: stage 7 fetcher/clone helpers, never raw sockets).
    touches_network: bool = False

    def json_schema(self) -> dict[str, Any]:
        """An OpenAI/MCP-style function definition for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
        }

    def parse(self, payload: Mapping[str, Any] | BaseModel) -> BaseModel:
        """Validate ``payload`` against the input model."""
        if isinstance(payload, self.input_model):
            return payload
        if isinstance(payload, BaseModel):
            payload = payload.model_dump()
        try:
            return self.input_model.model_validate(dict(payload))
        except ValidationError as exc:
            raise ToolInputError(f"invalid arguments for tool '{self.name}': {exc}") from exc

    def run(
        self,
        payload: Mapping[str, Any] | BaseModel,
        ctx: ToolContext | None = None,
    ) -> Any:
        """Validate, execute and trace one call.

        Raises:
            ToolInputError: the payload does not satisfy the input model.
            ToolError: the underlying capability failed (already-typed errors
                such as ``OutOfNetworkError`` pass through unchanged; anything
                else is wrapped in :class:`ToolExecutionError`).
        """
        args = self.parse(payload)
        context = ctx if ctx is not None else ToolContext()
        started = time.monotonic()
        try:
            result = self.handler(args, context)
        except ToolError as exc:
            self._record(context, args, started, error=exc)
            raise
        except Exception as exc:  # noqa: BLE001 - normalised below
            wrapped = ToolExecutionError(f"tool '{self.name}' failed: {exc}")
            self._record(context, args, started, error=wrapped)
            raise wrapped from exc
        self._record(context, args, started, error=None)
        return result

    def _record(
        self,
        ctx: ToolContext,
        args: BaseModel,
        started: float,
        *,
        error: Exception | None,
    ) -> None:
        # Iterate the model instead of dumping it: some inputs carry a whole
        # report and evidence bundle, and serialising megabytes of payload only
        # to discard everything non-scalar is a real cost on every call.
        params = {
            name: value for name, value in args if isinstance(value, str | int | float | bool)
        }
        ctx.trace.record(
            tool=self.name,
            target=_target_of(params),
            status="error" if error else "ok",
            latency_ms=(time.monotonic() - started) * 1000.0,
            error=str(error) if error else None,
            params=params,
            plan_step_id=ctx.active_plan_step_id,
            attempt=ctx.active_attempt,
        )


def _target_of(params: Mapping[str, Any]) -> str:
    """Pick the most identifying scalar argument for the trace line."""
    for key in ("url", "repo", "db", "dependency", "source_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return "-"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class ToolRegistry:
    """An ordered, name-addressed collection of :class:`Tool` values."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "(none)"
            raise ToolInputError(f"unknown tool: {name!r} (known: {known})") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        """Function definitions for every tool, ready for function calling."""
        return [self._tools[name].json_schema() for name in self.names()]

    def run(
        self,
        name: str,
        payload: Mapping[str, Any] | BaseModel,
        ctx: ToolContext | None = None,
    ) -> Any:
        return self.get(name).run(payload, ctx)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools[name] for name in self.names())

    def __len__(self) -> int:
        return len(self._tools)


# --------------------------------------------------------------------------- #
# Input models
# --------------------------------------------------------------------------- #


class CloneRepoInput(BaseModel):
    url: str = Field(description="https://github.com/<owner>/<repo> URL to clone.")
    ref: str | None = Field(default=None, description="Branch or tag to check out.")


class ScanDependencyInput(BaseModel):
    repo: str = Field(description="Local repository root to scan.")
    dependency: str = Field(description="Dependency name (any casing).")
    target_version: str = Field(description="Target PEP 440 version.")
    manifest: str | None = Field(
        default=None, description="Optional single manifest path, relative to repo."
    )


class ScanCodeInput(BaseModel):
    repo: str = Field(description="Local repository root to scan.")
    dependency: str = Field(description="Dependency name (any casing).")


class ResolveSkillInput(BaseModel):
    dependency: str = Field(description="Dependency name (any casing).")
    target_version: str | None = Field(
        default=None, description="Target version spec used to narrow the Skill Pack match."
    )


class RetrieveDocsInput(BaseModel):
    db: str = Field(description="SQLite evidence store containing ingested docs.")
    source_id: str = Field(description="Documentation source id to query.")
    query: str = Field(description="Keyword query.")
    top_k: int = Field(default=5, ge=1, le=50, description="Maximum evidence chunks to return.")


class RetrieveForPackageInput(BaseModel):
    db: str = Field(description="SQLite evidence store with ingested docs.")
    package: str = Field(description="Dependency name (any casing).")
    source_version: str = Field(default="", description="Resolved from-version spec, if known.")
    target_version: str = Field(default="", description="Target version spec.")
    user_intent: str = Field(default="", description="Free-text upgrade intent.")
    code_symbols: list[str] = Field(default_factory=list, description="Symbols discovered in code.")
    source_id: str | None = Field(default=None, description="Restrict to one doc source id.")
    top_k: int = Field(default=5, ge=1, le=50, description="Max chunks per query.")


class VerifyReportInput(BaseModel):
    repo: str = Field(description="Repository root the report refers to.")
    report: dict[str, Any] = Field(description="ImpactReport as JSON.")
    bundle: list[dict[str, Any]] = Field(
        description="Evidence items as JSON, in the order they were collected."
    )
    code_report: dict[str, Any] = Field(description="CodeEvidenceReport as JSON.")
    skill_id: str | None = Field(default=None, description="Skill Pack id to apply, if any.")
    degradations: list[str] = Field(
        default_factory=list, description="Known degradations to carry into the verdict."
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def resolve_skill_package(dependency: str, target_version_spec: str | None) -> SkillPackage | None:
    """Find the Skill Pack serving ``dependency`` (shared by CLI, MCP and tools)."""
    registry = builtin_registry()
    concrete = extract_version(target_version_spec or "")
    if concrete:
        try:
            selection = registry.select_skill(dependency, concrete)
        except SkillParseError:
            selection = None
        if selection is not None:
            found = registry.get(selection.skill_id)
            if found is not None:
                return found
    canonical = canonicalize_name(dependency.strip())
    for skill in registry.all():
        if canonical in skill.canonical_package_names:
            return skill
    return None


def _handle_clone_repo(args: CloneRepoInput, ctx: ToolContext) -> dict[str, Any]:
    handle = ctx.track_clone(clone_live_repo(args.url, args.ref, workdir=ctx.workdir))
    return {"path": str(handle.path), "slug": parse_repo_slug(args.url), "ref": args.ref or "main"}


def _handle_scan_dependency(args: ScanDependencyInput, ctx: ToolContext) -> dict[str, Any]:
    request = DependencyAnalysisRequest(
        repository_root=Path(args.repo),
        dependency_name=args.dependency,
        target_version=args.target_version,
        manifest_path=Path(args.manifest) if args.manifest else None,
    )
    return _scan_dependency(request).model_dump(mode="json")


def _handle_scan_code(args: ScanCodeInput, ctx: ToolContext) -> dict[str, Any]:
    report = scan_code_evidence(Path(args.repo), args.dependency)
    return report.model_dump(mode="json")


def _handle_resolve_skill(args: ResolveSkillInput, ctx: ToolContext) -> dict[str, Any]:
    skill = resolve_skill_package(args.dependency, args.target_version)
    if skill is None:
        return {"skill_id": None, "matched": False}
    return {
        "skill_id": skill.skill_id,
        "matched": True,
        "target_version_spec": skill.target_version_spec,
        "allow_patch_draft": skill.allow_patch_draft,
        "patterns": [p.id for p in skill.patterns],
    }


def _handle_retrieve_docs(args: RetrieveDocsInput, ctx: ToolContext) -> Any:
    session = ctx.session(Path(args.db))
    run = _retrieve(session, args.source_id, args.query, top_k=args.top_k)
    return run.model_dump(mode="json")


def _handle_retrieve_for_package(args: RetrieveForPackageInput, ctx: ToolContext) -> Any:
    session = ctx.session(Path(args.db))
    mode = ctx.gateway.mode if ctx.gateway is not None else ModelMode.FAKE
    runs = _retrieve_for_package(
        session,
        package=args.package,
        source_version=args.source_version,
        target_version=args.target_version,
        user_intent=args.user_intent,
        code_symbols=list(args.code_symbols),
        source_id=args.source_id,
        top_k=args.top_k,
        gateway=ctx.gateway,
        mode=mode,
        embedding=ctx.embedding,
    )
    return [run.model_dump(mode="json") for run in runs]


def _evidence_item_from_json(raw: Mapping[str, Any]) -> EvidenceItem:
    """Rebuild an :class:`EvidenceItem` (a plain dataclass, not a pydantic model)."""
    try:
        return EvidenceItem(
            evidence_id=str(raw["evidence_id"]),
            kind=str(raw["kind"]),
            summary=str(raw.get("summary", "")),
            detail=str(raw.get("detail", "")),
            meta=dict(raw.get("meta") or {}),
        )
    except (KeyError, TypeError) as exc:
        raise ToolInputError(f"malformed evidence item: {exc}") from exc


def _handle_verify_report(args: VerifyReportInput, ctx: ToolContext) -> dict[str, Any]:
    try:
        report = ImpactReport.model_validate(args.report)
        code_report = CodeEvidenceReport.model_validate(args.code_report)
    except ValidationError as exc:
        raise ToolInputError(f"verify_report received malformed payloads: {exc}") from exc
    bundle = EvidenceBundle([_evidence_item_from_json(item) for item in args.bundle])
    skill = builtin_registry().get(args.skill_id) if args.skill_id else None
    verified = _verify_report(
        report,
        repo_root=Path(args.repo),
        bundle=bundle,
        code_report=code_report,
        skill=skill,
        degradations=list(args.degradations),
    )
    return verified.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Built-in tools
# --------------------------------------------------------------------------- #

BUILTIN_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="clone_repo",
        description=(
            "Shallow-clone a public GitHub repository into a temp dir and return its "
            "local path. Only https://github.com/<owner>/<repo> URLs are accepted."
        ),
        input_model=CloneRepoInput,
        handler=_handle_clone_repo,
        touches_network=True,
    ),
    Tool(
        name="scan_dependency",
        description=(
            "Stage 1: report how a dependency is declared across the repository's "
            "manifests and how those constraints compare to a target version."
        ),
        input_model=ScanDependencyInput,
        handler=_handle_scan_dependency,
    ),
    Tool(
        name="scan_code",
        description=(
            "Stage 2: AST-scan the repository for usages of a dependency and return "
            "citable code evidence (imports, calls, attribute access)."
        ),
        input_model=ScanCodeInput,
        handler=_handle_scan_code,
    ),
    Tool(
        name="resolve_skill",
        description=(
            "Stage 3: pick the Skill Pack that serves a dependency/target version, "
            "returning its id and usage pattern ids."
        ),
        input_model=ResolveSkillInput,
        handler=_handle_resolve_skill,
    ),
    Tool(
        name="retrieve_docs",
        description=(
            "Stage 4: keyword-retrieve documentation chunks from the SQLite evidence "
            "store, returning ranked, citable doc evidence."
        ),
        input_model=RetrieveDocsInput,
        handler=_handle_retrieve_docs,
    ),
    Tool(
        name="retrieve_for_package",
        description=(
            "Stage 4: retrieve documentation chunks for a dependency upgrade from the "
            "shared corpus (FTS5 + optional vector RRF), returning ranked, citable doc "
            "evidence. Uses the same retrieval path as the deterministic pipeline -- "
            "prefer this over the old single-source retrieve_docs."
        ),
        input_model=RetrieveForPackageInput,
        handler=_handle_retrieve_for_package,
    ),
    Tool(
        name="verify_report",
        description=(
            "Stage 6: re-check an impact report against the evidence bundle and the "
            "repository, returning per-risk verdicts and the confidence summary."
        ),
        input_model=VerifyReportInput,
        handler=_handle_verify_report,
    ),
)


def default_registry() -> ToolRegistry:
    """A registry populated with every built-in tool."""
    return ToolRegistry(list(BUILTIN_TOOLS))


__all__ = [
    "BUILTIN_TOOLS",
    "CloneRepoInput",
    "ResolveSkillInput",
    "RetrieveDocsInput",
    "RetrieveForPackageInput",
    "ScanCodeInput",
    "ScanDependencyInput",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "VerifyReportInput",
    "default_registry",
    "resolve_skill_package",
]
