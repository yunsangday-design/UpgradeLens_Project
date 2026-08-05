"""Version comparison and aggregation tests (plan sections 11.4 and 8.7-8.9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.analyzers import compare_versions, exact_version_of, scan_repository
from upgradelens.domain import IssueCode, ResolutionStatus, VersionTransitionKind


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [
        ("==1.10.13", "1.10.13"),
        ("== 1.10.13", "1.10.13"),
        ("==2.0.0", "2.0.0"),
        ("==1.10.*", None),
        ("===1.10.13", None),
        (">=1.10", None),
        (">=1.10,<2", None),
        ("!=1.9", None),
        ("~=1.10.0", None),
        ("", None),
        ("==1.10.13,>=1.0", None),
    ],
)
def test_exact_version_of(specifier: str, expected: str | None) -> None:
    assert exact_version_of(specifier) == expected


def test_exact_version_of_rejects_garbage() -> None:
    assert exact_version_of("not a specifier") is None


@pytest.mark.parametrize(
    ("current", "target", "kind", "cross_major"),
    [
        ("1.10.13", "2.0.0", VersionTransitionKind.UPGRADE, True),
        ("1.10.13", "1.10.14", VersionTransitionKind.UPGRADE, False),
        ("2.0.0", "1.10.13", VersionTransitionKind.DOWNGRADE, True),
        ("1.10.13", "1.10.13", VersionTransitionKind.SAME, False),
        ("1.10.13", "1.10.13.0", VersionTransitionKind.SAME, False),
        ("0.9.0", "1.0.0", VersionTransitionKind.UPGRADE, True),
        ("2.0.0rc1", "2.0.0", VersionTransitionKind.UPGRADE, False),
    ],
)
def test_compare_versions(
    current: str, target: str, kind: VersionTransitionKind, cross_major: bool
) -> None:
    transition = compare_versions(current, target)
    assert transition.kind is kind
    assert transition.cross_major is cross_major
    assert transition.current_version == current
    assert transition.target_version == target


def test_compare_versions_without_current_is_unknown() -> None:
    transition = compare_versions(None, "2.0.0")
    assert transition.kind is VersionTransitionKind.UNKNOWN
    assert transition.cross_major is None


def test_compare_versions_with_unparsable_current_is_unknown() -> None:
    transition = compare_versions("not-a-version", "2.0.0")
    assert transition.kind is VersionTransitionKind.UNKNOWN
    assert transition.cross_major is None


def _scan(
    root: Path, dependency: str = "pydantic", target: str = "2.0.0", manifest: str | None = None
):
    return scan_repository(
        repository_root=root,
        dependency_name=dependency,
        target_version=target,
        manifest_path=Path(manifest) if manifest else None,
    )


def test_no_manifest_is_not_found(tmp_path: Path) -> None:
    result = _scan(tmp_path)
    assert result.status is ResolutionStatus.NOT_FOUND
    assert [e.code for e in result.errors] == [IssueCode.MANIFEST_NOT_FOUND]


def test_manifest_present_but_dependency_absent(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.status is ResolutionStatus.NOT_FOUND
    assert [w.code for w in result.warnings] == [IssueCode.DEPENDENCY_NOT_FOUND]
    assert result.errors == []


def test_all_manifests_unparsable_is_invalid(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.status is ResolutionStatus.INVALID
    assert [e.code for e in result.errors] == [IssueCode.INVALID_TOML]


def test_explicit_missing_manifest_is_not_found(tmp_path: Path) -> None:
    result = _scan(tmp_path, manifest="requirements.txt")
    assert result.status is ResolutionStatus.NOT_FOUND
    assert [e.code for e in result.errors] == [IssueCode.MANIFEST_NOT_FOUND]


def test_explicit_unsupported_manifest(tmp_path: Path) -> None:
    (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
    result = _scan(tmp_path, manifest="Pipfile")
    assert result.status is ResolutionStatus.UNSUPPORTED
    assert [e.code for e in result.errors] == [IssueCode.UNSUPPORTED_DECLARATION]


def test_explicit_pyproject_manifest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d"\nversion = "0"\ndependencies = ["pydantic==1.9.0"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")

    result = _scan(tmp_path, manifest="pyproject.toml")

    assert result.status is ResolutionStatus.RESOLVED
    assert result.current_version == "1.9.0"
    assert [d.manifest_type.value for d in result.declarations] == ["pyproject_toml"]


def test_absolute_explicit_manifest_is_accepted(tmp_path: Path) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("pydantic==1.10.13\n", encoding="utf-8")

    result = _scan(tmp_path, manifest=str(manifest))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.declarations[0].path == "requirements.txt"


def test_explicit_manifest_narrows_the_scan(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d"\nversion = "0"\ndependencies = ["pydantic==1.9.0"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")

    result = _scan(tmp_path, manifest="requirements.txt")

    assert result.status is ResolutionStatus.RESOLVED
    assert result.current_version == "1.10.13"
    assert len(result.declarations) == 1


def test_manifest_discovery_order_is_fixed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d"\nversion = "0"\ndependencies = ["pydantic==1.10.13"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")

    result = _scan(tmp_path)

    assert [d.manifest_type.value for d in result.declarations] == [
        "pyproject_toml",
        "requirements_txt",
    ]
    assert result.status is ResolutionStatus.RESOLVED
    assert [w.code for w in result.warnings] == [IssueCode.DUPLICATE_DECLARATION]


def test_conflicting_pins_are_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d"\nversion = "0"\ndependencies = ["pydantic==1.9.0"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")

    result = _scan(tmp_path)

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.current_version is None
    assert result.current_specifier is None
    assert [w.code for w in result.warnings] == [
        IssueCode.CONFLICTING_DECLARATIONS,
        IssueCode.AMBIGUOUS_SPECIFIER,
    ]


def test_range_is_ambiguous_and_reports_no_transition(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pydantic>=1.10,<2\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.current_version is None
    assert result.transition is VersionTransitionKind.UNKNOWN
    assert result.cross_major is None


def test_parse_errors_do_not_hide_a_resolved_dependency(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "===broken===\npydantic==1.10.13\n", encoding="utf-8"
    )
    result = _scan(tmp_path)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.current_version == "1.10.13"
    assert [e.code for e in result.errors] == [IssueCode.INVALID_DECLARATION]


def test_requested_name_is_preserved_and_canonicalised(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("Py_Dantic==1.10.13\n", encoding="utf-8")
    result = _scan(tmp_path, dependency="PY-DANTIC")
    assert result.requested_name == "PY-DANTIC"
    assert result.dependency_name == "py-dantic"
    assert result.declarations[0].raw_name == "Py_Dantic"


def test_downgrade_is_reported(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pydantic==2.5.0\n", encoding="utf-8")
    result = _scan(tmp_path, target="1.10.13")
    assert result.transition is VersionTransitionKind.DOWNGRADE
    assert result.cross_major is True
