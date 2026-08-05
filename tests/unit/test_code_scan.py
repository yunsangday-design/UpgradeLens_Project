"""Unit tests for the stage 2 AST code-evidence scanner.

These tests build tiny repositories in ``tmp_path`` and assert the scanner
behaves correctly on the tricky cases called out by the stage 2 plan: many
import forms, aliases, same-name shadowing, syntax errors, dynamic imports and
exact line numbers. All scanning is offline and never executes the fixtures.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.analyzers import scan_code_evidence
from upgradelens.domain import UsageKind

REPO_ROOT = "pydantic"


def _write(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _usages(report):
    return {(u.path, u.start_line, u.kind, u.symbol) for u in report.usages}


def test_multiple_import_forms_are_recorded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "app.py",
        "import pydantic\n"
        "import pydantic as pyd\n"
        "from pydantic import BaseModel\n"
        "from pydantic import BaseModel as BM\n"
        "from pydantic.v1 import BaseModel as V1\n"
        "import pydantic.v1 as pv\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    imports = [u for u in report.usages if u.kind is UsageKind.IMPORT]
    assert len(imports) == 6
    bound_as = {u.bound_as for u in imports}
    assert bound_as == {"pydantic", "pyd", "BaseModel", "BM", "V1", "pv"}
    # sub-module import forms still resolve to the pydantic dependency
    assert all(u.confidence == "high" for u in imports)


def test_alias_attribute_and_class_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "models.py",
        "import pydantic as pyd\n"
        "\n"
        "class User(pyd.BaseModel):\n"
        "    pass\n"
        "\n"
        "x = pyd.Field(description='x')\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    kinds = {u.kind for u in report.usages}
    assert UsageKind.CLASS_BASE in kinds
    assert UsageKind.CALL in kinds  # pyd.Field(...)
    base = next(u for u in report.usages if u.kind is UsageKind.CLASS_BASE)
    assert base.symbol == "BaseModel"
    assert base.bound_as == "pyd"
    assert base.confidence == "high"


def test_from_import_decorator_and_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "models.py",
        "from pydantic import BaseModel, validator\n"
        "\n"
        "class User(BaseModel):\n"
        "    pass\n"
        "\n"
        "@validator('name')\n"
        "def check(v):\n"
        "    return v\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    assert UsageKind.IMPORT in {u.kind for u in report.usages}
    base = next(u for u in report.usages if u.kind is UsageKind.CLASS_BASE)
    assert base.symbol == "BaseModel"
    dec = next(u for u in report.usages if u.kind is UsageKind.DECORATOR)
    assert dec.symbol == "validator"
    # the decorator call is not double-counted as a plain CALL
    assert not any(u.kind is UsageKind.CALL and u.symbol == "validator" for u in report.usages)


def test_same_name_shadow_marks_low_confidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "conf.py",
        "import pydantic\npydantic = load_config()\nx = pydantic.BaseModel\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    low = [u for u in report.usages if u.confidence == "low"]
    assert low, "shadowed import should produce low-confidence usages"
    assert any(u.kind is UsageKind.IMPORT and u.bound_as == "pydantic" for u in low)
    attr = next(
        u for u in report.usages if u.kind is UsageKind.ATTRIBUTE and u.symbol == "BaseModel"
    )
    assert attr.confidence == "low"
    assert report.summary.shadowed_binding_count >= 1


def test_syntax_error_is_recorded_not_fatal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "broken.py", "def (:\n    pass\n")
    _write(repo, "good.py", "from pydantic import BaseModel\nclass A(BaseModel):\n    pass\n")

    report = scan_code_evidence(repo, "pydantic")

    assert len(report.parse_errors) == 1
    assert report.parse_errors[0].path == "broken.py"
    # broken.py contributes no usages, but good.py still scanned
    assert all(u.path != "broken.py" for u in report.usages)
    assert any(u.path == "good.py" for u in report.usages)
    # no machine path leaks into the error message
    assert "/" not in report.parse_errors[0].message


def test_dynamic_imports_flagged_separately(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "dynamic.py",
        "import importlib\n"
        "m = importlib.import_module('pydantic')\n"
        "n = __import__('pydantic')\n"
        "o = __import__('os')\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    mechanisms = {(d.mechanism, d.resolved_name) for d in report.dynamic_imports}
    assert ("importlib.import_module", "pydantic") in mechanisms
    assert ("__import__", "pydantic") in mechanisms
    # an unrelated dynamic import is not evidence for our dependency
    assert ("__import__", "os") not in mechanisms
    # dynamic imports are NOT counted as normal CALL usages
    assert not any(u.kind is UsageKind.CALL for u in report.usages)


def test_line_numbers_and_snippets_are_exact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "app.py",
        "import pydantic\n\nfrom pydantic import BaseModel\n\n\nclass User(BaseModel):\n    pass\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    base = next(u for u in report.usages if u.kind is UsageKind.CLASS_BASE)
    # BaseModel is on line 6 (import pydantic l1, blank l2, from-import l3,
    # blank l4, blank l5, class l6)
    assert base.start_line == 6
    assert base.end_line == 6
    assert base.snippet == "class User(BaseModel):"
    # snippet must really exist at that line in the source
    lines = (repo / "app.py").read_text(encoding="utf-8").splitlines()
    assert lines[base.start_line - 1] == base.snippet


def test_venv_and_site_packages_excluded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "app.py", "from pydantic import BaseModel\nclass A(BaseModel):\n    pass\n")
    _write(
        repo,
        ".venv/lib/site-packages/pydantic/__init__.py",
        "from pydantic import BaseModel\nclass A(BaseModel):\n    pass\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    assert all(".venv" not in u.path for u in report.usages)
    assert all(".venv" not in p.path for p in report.parse_errors)
    assert report.scanned_files == 1
    assert any(u.path == "app.py" for u in report.usages)


def test_production_and_test_code_marked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "models.py", "from pydantic import BaseModel\nclass A(BaseModel):\n    pass\n")
    _write(
        repo,
        "tests/test_models.py",
        "from pydantic import BaseModel\nclass B(BaseModel):\n    pass\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    test_usages = [u for u in report.usages if u.is_test_code]
    assert test_usages, "usages inside tests/ should be flagged as test code"
    assert all(u.path.startswith("tests/") for u in test_usages)
    prod_usages = [u for u in report.usages if not u.is_test_code]
    assert all(u.path == "models.py" for u in prod_usages)
    # basic test -> production link uses the filename stem heuristic
    assert any(link.test_path == "tests/test_models.py" for link in report.test_production_links)


def test_canonical_name_matching_is_case_insensitive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "app.py",
        "import Pydantic\nfrom PYDANTIC import BaseModel\nclass A(BaseModel):\n    pass\n",
    )
    report = scan_code_evidence(repo, "pydantic")

    assert any(u.kind is UsageKind.CLASS_BASE and u.symbol == "BaseModel" for u in report.usages)
    assert report.dependency_name == "pydantic"
