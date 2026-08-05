"""Fixture contract test for the stage 2 code-evidence scanner.

Drives the ``pydantic_usage`` fixture repository (see its README) and asserts
the acceptance bar from the stage 2 plan:

- Recall of intended usages >= 90% (here 100% by construction);
- dynamic imports are flagged separately, not as normal usages;
- a syntax-error file becomes a ParseError and contributes 0 usages;
- every reported usage points at a real (path, line) -- no hallucination.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.analyzers import scan_code_evidence
from upgradelens.domain import UsageKind

FIXTURE_REPO = Path(__file__).resolve().parent.parent / "fixtures" / "pydantic_usage" / "repo"

# (path, kind, symbol, bound_as) fingerprints that the fixture intentionally
# contains. ``symbol`` is the imported entity (module name for a plain
# ``import x as y``; the imported name for ``from x import y as z``); ``bound_as``
# is the local alias actually used in source.
EXPECTED = {
    ("src/models.py", UsageKind.IMPORT, "BaseModel", "BaseModel"),
    ("src/models.py", UsageKind.IMPORT, "validator", "validator"),
    ("src/models.py", UsageKind.IMPORT, "root_validator", "root_validator"),
    ("src/models.py", UsageKind.CLASS_BASE, "BaseModel", "BaseModel"),
    ("src/models.py", UsageKind.CLASS_CONFIG, "Config", None),
    ("src/models.py", UsageKind.DECORATOR, "validator", "validator"),
    ("src/models.py", UsageKind.DECORATOR, "root_validator", "root_validator"),
    ("src/advanced.py", UsageKind.IMPORT, "pydantic", "pydantic"),
    ("src/advanced.py", UsageKind.IMPORT, "pydantic", "pyd"),
    ("src/advanced.py", UsageKind.ATTRIBUTE, "BaseModel", "pyd"),
    ("src/advanced.py", UsageKind.ATTRIBUTE, "Field", "pyd"),
    ("src/advanced.py", UsageKind.ATTRIBUTE, "VERSION", "pydantic"),
    ("src/advanced.py", UsageKind.CLASS_BASE, "BaseSettings", "pydantic"),
    ("src/with_alias.py", UsageKind.IMPORT, "BaseModel", "BM"),
    ("src/with_alias.py", UsageKind.CLASS_BASE, "BaseModel", "BM"),
    ("src/shadowed.py", UsageKind.IMPORT, "pydantic", "pydantic"),
    ("src/shadowed.py", UsageKind.ATTRIBUTE, "BaseModel", "pydantic"),
    ("tests/test_models.py", UsageKind.IMPORT, "BaseModel", "BaseModel"),
    ("tests/test_models.py", UsageKind.CLASS_BASE, "BaseModel", "BaseModel"),
}


def _fingerprints(report):
    return {(u.path, u.kind, u.symbol, u.bound_as) for u in report.usages}


def test_recall_meets_threshold() -> None:
    report = scan_code_evidence(FIXTURE_REPO, "pydantic")
    found = _fingerprints(report)
    missing = EXPECTED - found
    recall = len(EXPECTED & found) / len(EXPECTED)
    assert not missing, f"missing expected usages: {sorted(missing)}"
    assert recall >= 0.9


def test_dynamic_import_flagged_separately() -> None:
    report = scan_code_evidence(FIXTURE_REPO, "pydantic")
    assert any(
        d.mechanism == "importlib.import_module" and d.resolved_name == "pydantic"
        for d in report.dynamic_imports
    )
    assert not any(u.kind is UsageKind.CALL and u.symbol == "import_module" for u in report.usages)


def test_syntax_error_is_parse_error_not_fatal() -> None:
    report = scan_code_evidence(FIXTURE_REPO, "pydantic")
    assert any(pe.path == "broken_syntax.py" for pe in report.parse_errors)
    assert all(u.path != "broken_syntax.py" for u in report.usages)


def test_no_hallucinated_locations() -> None:
    report = scan_code_evidence(FIXTURE_REPO, "pydantic")
    assert report.usages, "fixture should yield usages"
    for usage in report.usages:
        file_path = FIXTURE_REPO / usage.path
        assert file_path.is_file(), f"usage points at missing file: {usage.path}"
        line_count = len(file_path.read_text(encoding="utf-8").splitlines())
        assert 1 <= usage.start_line <= line_count, (
            f"line {usage.start_line} out of range for {usage.path}"
        )


def test_test_code_and_kind_distribution() -> None:
    report = scan_code_evidence(FIXTURE_REPO, "pydantic")
    assert any(u.is_test_code and u.path.startswith("tests/") for u in report.usages)
    for kind in {
        UsageKind.IMPORT,
        UsageKind.CLASS_BASE,
        UsageKind.CLASS_CONFIG,
        UsageKind.DECORATOR,
        UsageKind.ATTRIBUTE,
    }:
        assert kind in report.summary.by_kind
    assert report.summary.shadowed_binding_count >= 1
    assert report.dependency_name == "pydantic"
