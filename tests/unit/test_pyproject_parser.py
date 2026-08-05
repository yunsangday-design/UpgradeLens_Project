"""pyproject.toml parser tests (plan section 11.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.utils import canonicalize_name

from upgradelens.analyzers import ManifestParseOutcome, parse_pyproject_toml
from upgradelens.domain import IssueCode, ManifestType


def _parse(tmp_path: Path, content: str, name: str = "pydantic") -> ManifestParseOutcome:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(content, encoding="utf-8")
    return parse_pyproject_toml(tmp_path, manifest, name)


_BASE = '[project]\nname = "demo"\nversion = "0.1.0"\n'


def test_exact_pin_uses_array_index_location(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = ["fastapi==0.95.2", "pydantic==1.10.13"]\n')
    assert len(outcome.declarations) == 1
    declaration = outcome.declarations[0]
    assert declaration.manifest_type is ManifestType.PYPROJECT_TOML
    assert declaration.path == "pyproject.toml"
    assert declaration.location == "[project].dependencies[1]"
    assert declaration.specifier == "==1.10.13"
    assert outcome.errors == []


def test_location_never_pretends_to_be_a_line_number(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = ["pydantic==1.10.13"]\n')
    assert not outcome.declarations[0].location.startswith("line:")


def test_range_specifier(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = ["pydantic>=1.10,<2"]\n')
    assert outcome.declarations[0].specifier == "<2,>=1.10"


def test_extras_and_marker(tmp_path: Path) -> None:
    outcome = _parse(
        tmp_path,
        _BASE + 'dependencies = ["pydantic[email]==1.10.13 ; python_version >= \\"3.8\\""]\n',
    )
    declaration = outcome.declarations[0]
    assert declaration.extras == ["email"]
    assert declaration.marker == 'python_version >= "3.8"'
    assert [w.code for w in outcome.warnings] == [IssueCode.MARKER_CONDITIONAL_DECLARATION]
    assert outcome.warnings[0].location == "[project].dependencies[0]"


@pytest.mark.parametrize("spelling", ["Pydantic", "PYDANTIC", "py_dantic"])
def test_name_normalisation(tmp_path: Path, spelling: str) -> None:
    outcome = _parse(
        tmp_path, _BASE + f'dependencies = ["{spelling}==1.0"]\n', canonicalize_name(spelling)
    )
    assert outcome.declarations[0].raw_name == spelling


def test_duplicate_entries_are_all_kept(tmp_path: Path) -> None:
    outcome = _parse(
        tmp_path, _BASE + 'dependencies = ["pydantic==1.10.13", "pydantic==1.10.13"]\n'
    )
    assert [d.location for d in outcome.declarations] == [
        "[project].dependencies[0]",
        "[project].dependencies[1]",
    ]


def test_missing_project_table(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, '[tool.poetry]\nname = "demo"\n')
    assert outcome.declarations == []
    assert [w.code for w in outcome.warnings] == [IssueCode.MISSING_PROJECT_TABLE]


def test_missing_dependencies_key(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE)
    assert outcome.declarations == []
    assert [w.code for w in outcome.warnings] == [IssueCode.MISSING_PROJECT_DEPENDENCIES]


def test_dynamic_dependencies_are_unsupported(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dynamic = ["dependencies"]\n')
    assert outcome.declarations == []
    assert [w.code for w in outcome.warnings] == [IssueCode.UNSUPPORTED_DECLARATION]
    assert "dynamic" in outcome.warnings[0].message


def test_dependencies_wrong_type(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = "pydantic==1.10.13"\n')
    assert outcome.declarations == []
    assert [e.code for e in outcome.errors] == [IssueCode.UNSUPPORTED_DEPENDENCIES_TYPE]


def test_dependency_entry_wrong_type(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = [123, "pydantic==1.10.13"]\n')
    assert len(outcome.declarations) == 1
    assert [e.code for e in outcome.errors] == [IssueCode.UNSUPPORTED_DEPENDENCIES_TYPE]
    assert outcome.errors[0].location == "[project].dependencies[0]"


def test_invalid_pep508_entry(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = ["===broken===", "pydantic==1.10.13"]\n')
    assert len(outcome.declarations) == 1
    assert outcome.declarations[0].location == "[project].dependencies[1]"
    assert [e.code for e in outcome.errors] == [IssueCode.INVALID_DECLARATION]
    assert outcome.errors[0].location == "[project].dependencies[0]"


def test_invalid_toml_is_a_file_level_error(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "[project\nname = broken\n")
    assert outcome.declarations == []
    assert [e.code for e in outcome.errors] == [IssueCode.INVALID_TOML]
    assert outcome.errors[0].location is None


def test_unreadable_path_is_a_file_level_error(tmp_path: Path) -> None:
    directory = tmp_path / "pyproject.toml"
    directory.mkdir()
    outcome = parse_pyproject_toml(tmp_path, directory, "pydantic")
    assert outcome.declarations == []
    assert [e.code for e in outcome.errors] == [IssueCode.INVALID_TOML]
    assert outcome.errors[0].location is None


def test_direct_url_reference_is_unsupported(tmp_path: Path) -> None:
    outcome = _parse(
        tmp_path, _BASE + 'dependencies = ["pydantic @ https://x.invalid/pydantic.whl"]\n'
    )
    assert outcome.declarations == []
    assert [w.code for w in outcome.warnings] == [IssueCode.UNSUPPORTED_DECLARATION]


def test_dependency_not_present(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _BASE + 'dependencies = ["fastapi==0.95.2"]\n')
    assert outcome.declarations == []
    assert outcome.warnings == []
    assert outcome.errors == []


def test_utf8_content(tmp_path: Path) -> None:
    content = _BASE + 'description = "中文描述"\ndependencies = ["pydantic==1.10.13"]\n'
    outcome = _parse(tmp_path, content)
    assert outcome.declarations[0].specifier == "==1.10.13"
