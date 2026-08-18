"""Stage S8: Test Intelligence unit tests.

Covers the horizontal ``upgradelens.testing`` package (profile / selection /
gap / proposal / verification) and its wiring into the PR-review, issue-repair,
security-review and dependency-upgrade capabilities.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.capabilities.pr_review.analyzers import (
    produce_test_gap_findings,
    review_pull_request,
)
from upgradelens.change.impact import ChangeImpact
from upgradelens.change.models import (
    ChangeHunk,
    ChangeLabel,
    ChangeSet,
    FileChange,
)
from upgradelens.core.action import ActionKind, TestProposal
from upgradelens.core.finding import FindingStatus
from upgradelens.repository.scan import scan_repository
from upgradelens.testing import (
    analyze_test_gaps,
    build_test_profile,
    generate_repro_test,
    generate_security_regression_test,
    recommend_regression_tests,
    select_tests,
    verify_test_proposal,
)


def _repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "\n".join(f"def add_{i}(a, b):\n    return a + b + {i}\n" for i in range(40))
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_add():\n    assert add(1, 2) == 3\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    return tmp_path


def _hunk(added: str) -> ChangeHunk:
    lines = [f"+{ln}" for ln in added.splitlines()] or ["+x"]
    return ChangeHunk(
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=1,
        lines=lines,
        additions=len(lines),
        deletions=0,
    )


def _change(path: str, *, added: str = "", label=ChangeLabel.MODIFIED) -> FileChange:
    return FileChange(
        path=path,
        label=label,
        hunks=[_hunk(added)],
        additions=1,
        deletions=0,
    )


def _cs(*specs) -> ChangeSet:
    files = []
    for spec in specs:
        if isinstance(spec, tuple):
            files.append(_change(spec[0], added=spec[1]))
        else:
            files.append(_change(spec))
    return ChangeSet(files=files)


def _impact() -> ChangeImpact:
    return ChangeImpact()


# --- profile ---------------------------------------------------------------


def test_build_test_profile_reads_pytest_config(tmp_path: Path):
    repo = _repo(tmp_path)
    info = build_test_profile(repo)
    assert info.framework == "pytest"
    assert any("test_app.py" in p for p in info.test_paths)
    assert info.pytest.testpaths == ["tests"]


# --- selection -------------------------------------------------------------


def test_select_tests_recommends_existing_or_proposes_new(tmp_path: Path):
    repo = _repo(tmp_path)
    profile = scan_repository(repo)
    sel = select_tests(_cs("src/app.py"), _impact(), profile)
    assert sel and not sel[0].is_new
    assert any("test_app.py" in t for t in sel[0].selected_tests)

    sel2 = select_tests(_cs("src/util.py"), _impact(), profile)
    assert sel2[0].is_new
    assert sel2[0].selected_tests == ["tests/test_util.py"]


def test_recommend_regression_tests_dedup(tmp_path: Path):
    repo = _repo(tmp_path)
    profile = scan_repository(repo)
    recs = recommend_regression_tests(_cs("src/app.py", "src/util.py"), _impact(), profile)
    assert any("test_app.py" in t for t in recs)
    assert any("test_util.py" in t for t in recs)
    assert len(recs) == len(set(recs))


# --- gap analysis ----------------------------------------------------------


def test_analyze_test_gaps_kinds(tmp_path: Path):
    repo = _repo(tmp_path)
    profile = scan_repository(repo)
    cs = _cs(
        ("src/new_feature.py", "x = 1\n"),
        ("src/risky.py", "raise ValueError('boom')\n"),
        ("src/boundary.py", "if x > 0:\n    pass\n"),
    )
    gaps = analyze_test_gaps(cs, profile)
    by_src = {g.source_path: g for g in gaps}
    assert "src/new_feature.py" in by_src
    assert by_src["src/new_feature.py"].kind.value == "missing_test"
    assert by_src["src/risky.py"].kind.value == "missing_exception_test"
    assert by_src["src/boundary.py"].kind.value == "missing_boundary_test"

    findings = [g.to_finding() for g in gaps]
    assert all(f.category == "test_gap" for f in findings)
    assert all(f.status is FindingStatus.CANDIDATE for f in findings)


# --- proposal --------------------------------------------------------------


def test_generate_repro_and_security_regression(tmp_path: Path):
    repo = _repo(tmp_path)
    repro = generate_repro_test(
        repo_root=repo, source_path="src/app.py", issue_text="add() returns wrong sum"
    )
    assert repro.kind is ActionKind.TEST
    assert repro.test_paths
    assert repro.metadata["verification_status"] == "proposed"
    assert "assert" in repro.metadata["test_code"]
    assert repro.intended_to_fail_before_fix is True

    class _FakeFinding:
        finding_id = "sec-1"
        title = "hardcoded secret"

    sec = generate_security_regression_test(
        repo_root=repo, source_path="src/app.py", finding=_FakeFinding()
    )
    assert "security_regression" in sec.metadata["kind"]
    assert sec.finding_ids == ["sec-1"]


# --- verification ----------------------------------------------------------


def test_verifier_invalid_when_trivial():
    trivial = TestProposal(
        proposal_id="t1",
        kind=ActionKind.TEST,
        finding_ids=[],
        title="x",
        test_paths=["tests/test_x.py"],
        metadata={"test_code": "def test_x():\n    assert True\n"},
    )
    result = verify_test_proposal(trivial)
    assert result.passed is False
    assert "invalid" in result.summary.lower()


def test_verifier_proposed_offline(tmp_path: Path):
    repo = _repo(tmp_path)
    repro = generate_repro_test(repo_root=repo, source_path="src/app.py", issue_text="bug")
    result = verify_test_proposal(repro)  # runnable=False by default
    assert result.passed is False  # cannot be verified offline
    assert "proposed" in result.summary


def test_verifier_verified_when_executed(tmp_path: Path):
    repo = _repo(tmp_path)
    repro = generate_repro_test(repo_root=repo, source_path="src/app.py", issue_text="bug")
    result = verify_test_proposal(
        repro, runnable=True, before_pass=False, after_pass=True
    )
    assert result.passed is True
    assert "verified" in result.summary


# --- horizontal wiring -----------------------------------------------------


def test_pr_review_produces_test_gap_findings(tmp_path: Path):
    repo = _repo(tmp_path)
    profile = scan_repository(repo)
    cs = _cs(("src/uncovered.py", "y = 2\n"))
    findings = produce_test_gap_findings(cs, profile)
    assert any(f.category == "test_gap" for f in findings)


def test_review_pull_request_carries_test_gap_findings(tmp_path: Path):
    from upgradelens.llm.fixtures_core import build_fake_core_responses
    from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode

    repo = _repo(tmp_path)
    gw = ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )
    diff = (
        "diff --git a/src/new_feature.py b/src/new_feature.py\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/src/new_feature.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def new_feature():\n"
        "+    return 2\n"
    )
    result = review_pull_request(repo_root=repo, unified_diff=diff, gateway=gw)
    assert isinstance(result.test_gap_findings, list)
    assert any(f.category == "test_gap" for f in result.test_gap_findings)


def test_review_security_generates_regression_tests(tmp_path: Path):
    from upgradelens.capabilities.security_review.analyzers import review_security
    from upgradelens.llm.fixtures_core import build_fake_core_responses
    from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "def handler():\n    return 1\n\n\ndef new_feature():\n    return 2\n"
    )
    gw = ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )
    app_diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def handler():\n"
        "     return 1\n"
        "+\n"
        "+def new_feature():\n"
        "+    return 2\n"
    )
    result = review_security(repo_root=tmp_path, unified_diff=app_diff, gateway=gw)
    assert result.test_proposals
    assert all(tp.kind is ActionKind.TEST for tp in result.test_proposals)


def test_repair_issue_generates_repro_test(tmp_path: Path):
    from upgradelens.capabilities.issue_repair.analyzers import repair_issue
    from upgradelens.llm.fixtures_core import build_fake_core_responses
    from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("def handle(cfg):\n    return cfg.value\n")
    gw = ModelGateway(
        ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
        fake_responses=build_fake_core_responses(),
    )
    issue_text = (
        "# ISSUE-42: App crashes when config missing\n\n"
        "The handler dereferences cfg.value but cfg can be None.\n"
    )
    result = repair_issue(repo_root=tmp_path, issue_text=issue_text, gateway=gw)
    assert result.repro_tests
    assert all(tp.kind is ActionKind.TEST for tp in result.repro_tests)


def test_dependency_upgrade_exposes_regression_recommender():
    from upgradelens.capabilities.dependency_upgrade import recommend_regression_tests

    assert callable(recommend_regression_tests)
