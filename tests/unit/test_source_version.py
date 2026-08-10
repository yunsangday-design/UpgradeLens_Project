"""S1: the from-version is inferred from the manifest and threaded everywhere.

Before S1 the pipeline used ``getattr(code_report, "version", "")`` which is
always empty (``CodeEvidenceReport`` has no ``version`` field), so the source
version was silently dropped. S1 runs ``scan_dependency`` first and turns the
result into a :class:`SourceVersion` that flows into RAG queries, the planner /
impact prompts, the verified report, and the scan trace.
"""

from __future__ import annotations

from upgradelens.domain import DependencyScanResult, ResolutionStatus
from upgradelens.models.impact import SourceVersion
from upgradelens.pipeline import (
    AssessmentRequest,
    _resolve_source_version,
    collect_evidence,
)
from upgradelens.tools.registry import ToolContext, default_registry


def _scan(**overrides: object) -> DependencyScanResult:
    data: dict[str, object] = {
        "requested_name": "pydantic",
        "dependency_name": "pydantic",
        "status": "resolved",
        "target_version": "2.0",
    }
    data.update(overrides)
    return DependencyScanResult.model_validate(data)


def test_user_source_version_overrides_the_scan() -> None:
    req = AssessmentRequest(repo=".", dependency="pydantic", source_version="1.10.0")
    sv = _resolve_source_version(req, None)
    assert sv == SourceVersion(spec="1.10.0", origin="user", status="declared")


def test_declared_exact_pin_resolves() -> None:
    scan = _scan(
        current_version="1.10.13",
        current_specifier="==1.10.13",
    )
    sv = _resolve_source_version(AssessmentRequest(repo=".", dependency="pydantic"), scan)
    assert sv.spec == "==1.10.13"
    assert sv.status == "declared"
    assert sv.origin == "declared"


def test_inferred_range_is_not_treated_as_a_pin() -> None:
    scan = _scan(
        status="ambiguous",
        current_specifier=">=1.10,<2",
    )
    sv = _resolve_source_version(AssessmentRequest(repo=".", dependency="pydantic"), scan)
    assert sv.spec == ">=1.10,<2"
    assert sv.status == "inferred"


def test_unknown_when_not_declared() -> None:
    scan = _scan(status="not_found")
    sv = _resolve_source_version(AssessmentRequest(repo=".", dependency="pydantic"), scan)
    assert sv.spec is None
    assert sv.status == "unknown"


def test_conflicting_declarations_detected() -> None:
    scan = _scan(
        status="ambiguous",
        warnings=[{"code": "conflicting_declarations", "message": "conflict"}],
    )
    sv = _resolve_source_version(AssessmentRequest(repo=".", dependency="pydantic"), scan)
    assert sv.spec is None
    assert sv.status == "conflict"


def test_source_version_label_is_human_readable() -> None:
    assert (
        SourceVersion(spec="==1.10.13", origin="declared", status="declared").label
        == "declared (==1.10.13)"
    )
    assert (
        SourceVersion(spec=">=1.10", origin="declared", status="inferred").label
        == "inferred (>=1.10)"
    )
    assert SourceVersion(spec=None, origin="declared", status="unknown").label.startswith("unknown")
    assert (
        SourceVersion(spec="1.10.0", origin="user", status="declared").label
        == "user-provided (1.10.0)"
    )


def _make_repo(tmp_path, dependency_line: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        f"dependencies = [{dependency_line!r}]\n"
    )
    return repo


def test_collect_evidence_threads_declared_pin(tmp_path) -> None:
    repo = _make_repo(tmp_path, "pydantic==1.10.13")
    ctx = ToolContext(workdir=tmp_path)
    request = AssessmentRequest(repo=str(repo), dependency="pydantic", target_version="2.0")
    collection = collect_evidence(request, ctx, registry=default_registry())
    ctx.close()

    assert collection.source_version is not None
    assert collection.source_version.status == "declared"
    assert collection.source_version.spec == "==1.10.13"
    assert collection.spec.source_version_spec == "==1.10.13"
    assert collection.spec.source_version is collection.source_version
    assert collection.dependency_scan is not None
    assert collection.dependency_scan.status == ResolutionStatus.RESOLVED

    decl = collection.bundle.by_kind("dependency_declaration")
    assert decl, "a dependency_declaration evidence item must be recorded"
    assert decl[0].meta["source_version_status"] == "declared"
    assert decl[0].meta["source_version_spec"] == "==1.10.13"


def test_collect_evidence_does_not_fabricate_an_unknown_source(tmp_path) -> None:
    repo = _make_repo(tmp_path, "requests>=2.0")  # pydantic is not declared here
    ctx = ToolContext(workdir=tmp_path)
    request = AssessmentRequest(repo=str(repo), dependency="pydantic", target_version="2.0")
    collection = collect_evidence(request, ctx, registry=default_registry())
    ctx.close()

    assert collection.source_version is not None
    assert collection.source_version.status == "unknown"
    assert collection.source_version.spec is None
    # The report must not invent a concrete from-version.
    assert collection.spec.source_version_spec == ""
    assert collection.dependency_scan is not None
    assert collection.dependency_scan.status == ResolutionStatus.NOT_FOUND


def test_collect_evidence_records_inferred_range(tmp_path) -> None:
    repo = _make_repo(tmp_path, "pydantic>=1.10,<2")
    ctx = ToolContext(workdir=tmp_path)
    request = AssessmentRequest(repo=str(repo), dependency="pydantic", target_version="2.0")
    collection = collect_evidence(request, ctx, registry=default_registry())
    ctx.close()

    assert collection.source_version is not None
    assert collection.source_version.status == "inferred"
    assert collection.source_version.spec is not None
    assert "1.10" in collection.source_version.spec
    decl = collection.bundle.by_kind("dependency_declaration")
    assert decl
    assert decl[0].meta["source_version_status"] == "inferred"
