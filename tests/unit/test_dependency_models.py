"""Domain model tests (plan section 11.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from upgradelens.domain import (
    SCHEMA_VERSION,
    DependencyAnalysisRequest,
    DependencyDeclaration,
    DependencyScanResult,
    IssueCode,
    ManifestType,
    ParseIssue,
    ResolutionStatus,
    VersionTransition,
    VersionTransitionKind,
)


@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_request_rejects_empty_dependency_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        DependencyAnalysisRequest(
            repository_root=tmp_path, dependency_name=name, target_version="2.0.0"
        )


@pytest.mark.parametrize("version", ["not-a-version", "2.0.0.x", "", "v 2"])
def test_request_rejects_invalid_target_version(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValidationError):
        DependencyAnalysisRequest(
            repository_root=tmp_path, dependency_name="pydantic", target_version=version
        )


def test_request_rejects_missing_repository_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        DependencyAnalysisRequest(
            repository_root=tmp_path / "nope",
            dependency_name="pydantic",
            target_version="2.0.0",
        )


def test_request_rejects_file_as_repository_root(tmp_path: Path) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("pydantic==1.10.13\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="not a directory"):
        DependencyAnalysisRequest(
            repository_root=manifest, dependency_name="pydantic", target_version="2.0.0"
        )


def test_request_normalises_name_and_keeps_original(tmp_path: Path) -> None:
    request = DependencyAnalysisRequest(
        repository_root=tmp_path,
        dependency_name="  PyDantic  ",
        target_version="2.0.0",
    )
    assert request.dependency_name == "PyDantic"
    assert request.canonical_name == "pydantic"


def test_declaration_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        DependencyDeclaration(  # type: ignore[call-arg]
            manifest_type=ManifestType.REQUIREMENTS_TXT,
            path="requirements.txt",
        )


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ParseIssue(  # type: ignore[call-arg]
            code=IssueCode.AMBIGUOUS_SPECIFIER,
            message="x",
            unexpected="boom",
        )


def test_issue_carries_location() -> None:
    issue = ParseIssue(
        code=IssueCode.INVALID_DECLARATION,
        message="bad line",
        manifest_type=ManifestType.REQUIREMENTS_TXT,
        path="requirements.txt",
        location="line:7",
    )
    dumped = issue.model_dump(mode="json")
    assert dumped == {
        "code": "invalid_declaration",
        "message": "bad line",
        "manifest_type": "requirements_txt",
        "path": "requirements.txt",
        "location": "line:7",
    }


def test_issue_location_is_optional() -> None:
    issue = ParseIssue(code=IssueCode.AMBIGUOUS_SPECIFIER, message="range")
    assert issue.model_dump(mode="json")["path"] is None


def test_enum_values_serialize_as_expected_strings() -> None:
    assert [m.value for m in ManifestType] == ["requirements_txt", "pyproject_toml"]
    assert [s.value for s in ResolutionStatus] == [
        "resolved",
        "ambiguous",
        "not_found",
        "invalid",
        "unsupported",
    ]
    assert [k.value for k in VersionTransitionKind] == [
        "upgrade",
        "downgrade",
        "same",
        "unknown",
    ]


def test_unknown_transition_helper() -> None:
    transition = VersionTransition.unknown("2.0.0")
    assert transition.kind is VersionTransitionKind.UNKNOWN
    assert transition.cross_major is None
    assert transition.current_version is None
    assert transition.target_version == "2.0.0"


def test_result_json_shape_is_stable() -> None:
    result = DependencyScanResult(
        requested_name="pydantic",
        dependency_name="pydantic",
        status=ResolutionStatus.RESOLVED,
        current_version="1.10.13",
        current_specifier="==1.10.13",
        target_version="2.0.0",
        transition=VersionTransitionKind.UPGRADE,
        cross_major=True,
        declarations=[
            DependencyDeclaration(
                manifest_type=ManifestType.REQUIREMENTS_TXT,
                path="requirements.txt",
                location="line:2",
                raw="pydantic==1.10.13",
                raw_name="pydantic",
                specifier="==1.10.13",
            )
        ],
    )
    dumped = result.model_dump(mode="json")
    assert list(dumped) == [
        "schema_version",
        "requested_name",
        "dependency_name",
        "status",
        "current_version",
        "current_specifier",
        "target_version",
        "transition",
        "cross_major",
        "declarations",
        "warnings",
        "errors",
    ]
    assert dumped["schema_version"] == SCHEMA_VERSION
    assert dumped["status"] == "resolved"
    assert dumped["transition"] == "upgrade"
    assert dumped["declarations"][0]["extras"] == []
    assert dumped["declarations"][0]["marker"] is None
    assert dumped["warnings"] == []
    assert dumped["errors"] == []


def test_result_is_immutable() -> None:
    result = DependencyScanResult(
        requested_name="pydantic",
        dependency_name="pydantic",
        status=ResolutionStatus.NOT_FOUND,
        target_version="2.0.0",
    )
    with pytest.raises(ValidationError):
        result.status = ResolutionStatus.RESOLVED  # type: ignore[misc]
