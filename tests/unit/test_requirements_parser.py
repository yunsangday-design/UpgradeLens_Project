"""requirements.txt parser tests (plan section 11.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.utils import canonicalize_name

from upgradelens.analyzers import ManifestParseOutcome, parse_requirements_txt
from upgradelens.domain import IssueCode, ManifestType


def _parse(
    tmp_path: Path, content: str, name: str = "pydantic", *, newline: str = "\n"
) -> ManifestParseOutcome:
    manifest = tmp_path / "requirements.txt"
    manifest.write_bytes(content.replace("\n", newline).encode("utf-8"))
    return parse_requirements_txt(tmp_path, manifest, name)


def test_exact_pin(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic==1.10.13\n")
    assert len(outcome.declarations) == 1
    declaration = outcome.declarations[0]
    assert declaration.manifest_type is ManifestType.REQUIREMENTS_TXT
    assert declaration.path == "requirements.txt"
    assert declaration.location == "line:1"
    assert declaration.specifier == "==1.10.13"
    assert declaration.raw == "pydantic==1.10.13"
    assert outcome.warnings == []
    assert outcome.errors == []


def test_range_specifier_is_canonicalised(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic>=1.10,<2\n")
    assert outcome.declarations[0].specifier == "<2,>=1.10"


def test_no_specifier(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic\n")
    assert outcome.declarations[0].specifier == ""


def test_extras_are_sorted(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic[email,dotenv]==1.10.13\n")
    assert outcome.declarations[0].extras == ["dotenv", "email"]


def test_marker_is_captured_and_warned(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, 'pydantic==1.10.13 ; python_version >= "3.8"\n')
    assert outcome.declarations[0].marker == 'python_version >= "3.8"'
    assert [w.code for w in outcome.warnings] == [IssueCode.MARKER_CONDITIONAL_DECLARATION]
    assert outcome.warnings[0].location == "line:1"


@pytest.mark.parametrize(
    "spelling", ["Pydantic", "PYDANTIC", "py_dantic", "py-dantic", "py.dantic"]
)
def test_name_normalisation(tmp_path: Path, spelling: str) -> None:
    outcome = _parse(tmp_path, f"{spelling}==1.0\n", canonicalize_name(spelling))
    assert len(outcome.declarations) == 1
    assert outcome.declarations[0].raw_name == spelling


def test_comment_and_blank_lines_are_skipped_but_counted(tmp_path: Path) -> None:
    content = "# first\n\n# third\npydantic==1.10.13\n"
    outcome = _parse(tmp_path, content)
    assert outcome.declarations[0].location == "line:4"


def test_trailing_comment_is_stripped(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic==1.10.13  # keep for now\n")
    assert outcome.declarations[0].raw == "pydantic==1.10.13"
    assert outcome.declarations[0].specifier == "==1.10.13"


def test_hash_without_preceding_space_is_not_a_comment(tmp_path: Path) -> None:
    """pip only treats ``#`` as a comment at line start or after whitespace.

    ``pydantic==1.10.13#egg=pydantic`` must therefore be handed to the PEP 508
    parser in full and rejected, rather than being silently truncated into a
    valid-looking ``pydantic==1.10.13`` pin.
    """
    outcome = _parse(tmp_path, "pydantic==1.10.13#egg=pydantic\n")
    assert outcome.declarations == []
    assert [e.code for e in outcome.errors] == [IssueCode.INVALID_DECLARATION]
    assert outcome.errors[0].location == "line:1"


def test_line_continuation_reports_start_line(tmp_path: Path) -> None:
    content = '# header\npydantic==1.10.13 \\\n    ; python_version >= "3.8"\n'
    outcome = _parse(tmp_path, content)
    assert outcome.declarations[0].location == "line:2"
    assert outcome.declarations[0].marker == 'python_version >= "3.8"'


def test_dangling_continuation_at_end_of_file(tmp_path: Path) -> None:
    """A file ending in ``\\`` must still yield its last declaration."""
    outcome = _parse(tmp_path, "pydantic==1.10.13 \\\n")
    assert outcome.declarations[0].location == "line:1"
    assert outcome.declarations[0].specifier == "==1.10.13"


def test_unreadable_path_is_a_file_level_error(tmp_path: Path) -> None:
    directory = tmp_path / "requirements.txt"
    directory.mkdir()
    outcome = parse_requirements_txt(tmp_path, directory, "pydantic")
    assert outcome.declarations == []
    assert [e.code for e in outcome.errors] == [IssueCode.INVALID_DECLARATION]
    assert outcome.errors[0].location is None


def test_hash_options_are_removed(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic==1.10.13 --hash=sha256:abc --hash=sha256:def\n")
    assert outcome.declarations[0].specifier == "==1.10.13"


def test_duplicate_declarations_are_all_kept(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic==1.10.13\nrequests==2.31.0\npydantic==1.10.13\n")
    assert [d.location for d in outcome.declarations] == ["line:1", "line:3"]


def test_invalid_line_is_reported_with_location(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic==1.10.13\n===broken===\n")
    assert len(outcome.declarations) == 1
    assert len(outcome.errors) == 1
    assert outcome.errors[0].code is IssueCode.INVALID_DECLARATION
    assert outcome.errors[0].location == "line:2"


@pytest.mark.parametrize(
    "line",
    ["-r base.txt", "-e .", "--index-url https://example.invalid/simple", "-c constraints.txt"],
)
def test_pip_option_lines_are_unsupported(tmp_path: Path, line: str) -> None:
    outcome = _parse(tmp_path, f"{line}\npydantic==1.10.13\n")
    assert [w.code for w in outcome.warnings] == [IssueCode.UNSUPPORTED_DECLARATION]
    assert outcome.warnings[0].location == "line:1"
    assert outcome.declarations[0].location == "line:2"


@pytest.mark.parametrize(
    "line",
    ["https://example.invalid/pydantic.whl", "./vendor/pydantic", "git+https://x.invalid/p.git"],
)
def test_url_and_path_entries_are_unsupported(tmp_path: Path, line: str) -> None:
    outcome = _parse(tmp_path, f"{line}\n")
    assert outcome.declarations == []
    assert [w.code for w in outcome.warnings] == [IssueCode.UNSUPPORTED_DECLARATION]


def test_direct_url_reference_is_unsupported(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "pydantic @ https://example.invalid/pydantic-1.10.13.whl\n")
    assert outcome.declarations == []
    assert [w.code for w in outcome.warnings] == [IssueCode.UNSUPPORTED_DECLARATION]


def test_dependency_not_present(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, "requests==2.31.0\nurllib3==2.0.0\n")
    assert outcome.declarations == []
    assert outcome.errors == []


def test_crlf_yields_same_result_as_lf(tmp_path: Path) -> None:
    lf_dir = tmp_path / "lf"
    crlf_dir = tmp_path / "crlf"
    lf_dir.mkdir()
    crlf_dir.mkdir()
    content = "# header\npydantic==1.10.13\n"

    lf = _parse(lf_dir, content)
    crlf = _parse(crlf_dir, content, newline="\r\n")

    assert lf.declarations == crlf.declarations
    assert crlf.declarations[0].location == "line:2"


def test_utf8_comments_do_not_break_parsing(tmp_path: Path) -> None:
    content = "# 中文注释：保留旧版本\npydantic==1.10.13  # 待升级\n"
    outcome = _parse(tmp_path, content)
    assert outcome.declarations[0].location == "line:2"
    assert outcome.declarations[0].raw == "pydantic==1.10.13"


def test_unreadable_manifest_is_a_file_level_error(tmp_path: Path) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_bytes(b"\xff\xfe\x00invalid utf-8")
    outcome = parse_requirements_txt(tmp_path, manifest, "pydantic")
    assert outcome.declarations == []
    assert len(outcome.errors) == 1
    assert outcome.errors[0].location is None
