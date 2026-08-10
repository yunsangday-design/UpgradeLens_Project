"""S2: the model-driven agent converges on the same evidence path as the
deterministic pipeline.

Before S2 the ReAct loop carried its own copy of the evidence-collection logic
(``retrieve_docs`` single-source retrieval, ``resolve_skill`` forced, and a
separate ``EvidenceCollection`` builder that dropped the source version). S2
routes the agent through the *same* shared entry points the pipeline uses, so
the two paths can no longer diverge in behaviour or in the evidence contract.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.agent.loop import _Accumulator, _build_collection, _collection_tool_specs
from upgradelens.domain import DependencyScanResult
from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeEvidenceSummary
from upgradelens.pipeline import AssessmentRequest, build_evidence_collection
from upgradelens.tools.registry import default_registry, resolve_skill_package


def _code_report() -> CodeEvidenceReport:
    return CodeEvidenceReport(
        dependency_name="frobnicate",
        scanned_files=0,
        summary=CodeEvidenceSummary(scanned_files=0, usage_count=0),
    )


def _scan(**overrides: object) -> DependencyScanResult:
    data: dict[str, object] = {
        "requested_name": "frobnicate",
        "dependency_name": "frobnicate",
        "status": "resolved",
        "target_version": "2.0",
        "current_version": "1.4.2",
        "current_specifier": ">=1.4,<2",
    }
    data.update(overrides)
    return DependencyScanResult.model_validate(data)


def _request(**overrides: object) -> AssessmentRequest:
    data = {
        "repo": "/tmp/repo",
        "dependency": "frobnicate",
        "target_version": "2.0",
        "db": "store.db",
    }
    data.update(overrides)
    return AssessmentRequest(**data)  # type: ignore[arg-type]


def test_local_repo_does_not_expose_clone_repo() -> None:
    registry = default_registry()
    req = AssessmentRequest(repo="/local/path", dependency="pydantic", target_version="2.0")
    local_names = [s["name"] for s in _collection_tool_specs(registry, req, repo_is_url=False)]
    url_names = [s["name"] for s in _collection_tool_specs(registry, req, repo_is_url=True)]
    assert "clone_repo" not in local_names
    assert "clone_repo" in url_names


def test_retrieve_for_package_allowed_with_shared_db_only() -> None:
    registry = default_registry()
    # A shared DB (even without a source_id) exposes the package-level retrieval.
    db_only = _request(source_id=None)
    assert "retrieve_for_package" in [
        s["name"] for s in _collection_tool_specs(registry, db_only, repo_is_url=False)
    ]
    # No doc store at all -> the agent must not try to retrieve.
    no_db = _request(db=None)
    assert "retrieve_for_package" not in [
        s["name"] for s in _collection_tool_specs(registry, no_db, repo_is_url=False)
    ]


def test_retrieve_for_package_tool_is_registered() -> None:
    spec = default_registry().get("retrieve_for_package").json_schema()
    assert spec["name"] == "retrieve_for_package"
    assert spec["parameters"]["type"] == "object"
    props = spec["parameters"]["properties"]
    assert "package" in props and "db" in props and "source_version" in props


def test_build_collection_carries_source_version_and_scan_result() -> None:
    acc = _Accumulator(
        repo_path=Path("/tmp/repo"),
        code_report=_code_report(),
        scan_result=_scan(),
        source_version_spec=">=1.4,<2",
        target_version_spec="2.0",
    )
    collection = _build_collection(acc, _request())
    assert collection.source_version is not None
    assert collection.source_version.spec == ">=1.4,<2"
    assert collection.source_version.status == "declared"
    assert collection.dependency_scan is acc.scan_result
    # A Skill Pack is optional; even with only the generic fallthrough skill
    # the agent completes without a forced resolve_skill step.
    assert collection.skill is not None


def test_agent_and_pipeline_build_identical_evidence() -> None:
    """Both paths feed the same ``build_evidence_collection`` builder, so the
    collected evidence contract (source version, scan result, spec, bundle,
    degradations) must be identical for the same inputs."""
    request = _request()
    code_report = _code_report()
    scan = _scan()

    acc = _Accumulator(
        repo_path=Path("/tmp/repo"),
        code_report=code_report,
        scan_result=scan,
        source_version_spec=">=1.4,<2",
        target_version_spec="2.0",
    )
    loop_collection = _build_collection(acc, request)

    # The deterministic pipeline resolves the same generic skill, so the two
    # paths must agree on the skill too.
    skill = resolve_skill_package(request.dependency, request.target_version)
    pipeline_collection = build_evidence_collection(
        request=request,
        repo_path=Path("/tmp/repo"),
        code_report=code_report,
        doc_runs=[],
        scan_result=scan,
        skill=skill,
        degradations=[],
    )

    assert (
        loop_collection.source_version.spec
        == pipeline_collection.source_version.spec
        == ">=1.4,<2"
    )
    assert (
        loop_collection.source_version.status
        == pipeline_collection.source_version.status
        == "declared"
    )
    assert loop_collection.dependency_scan is scan
    assert pipeline_collection.dependency_scan is scan
    assert (
        loop_collection.spec.target_version_spec
        == pipeline_collection.spec.target_version_spec
        == "2.0"
    )
    assert len(loop_collection.bundle.items) == len(pipeline_collection.bundle.items)
    assert loop_collection.degradations == pipeline_collection.degradations
