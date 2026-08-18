"""Test Proposal: generate a test patch for repro / regression / security (S8)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from upgradelens.core.action import ActionKind, TestProposal

__all__ = [
    "TestProposalSpec",
    "propose_test",
    "generate_repro_test",
    "generate_security_regression_test",
]


class TestProposalSpec(BaseModel):
    """Input spec for :func:`propose_test`."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    test_path: str
    kind: str = "regression"  # regression | repro | security_regression
    target_symbol: str = ""
    source_path: str = ""
    description: str = ""
    intended_to_fail_before_fix: bool = False


def _test_code(
    *,
    kind: str,
    test_path: str,
    target_symbol: str,
    source_path: str,
    note: str,
) -> str:
    func = f"test_{kind}_{Path(source_path).stem or 'subject'}"
    sym = target_symbol or Path(source_path).stem or "subject"
    if kind == "repro":
        body = (
            f"def {func}():\n"
            f'    """Reproduces the reported issue for {sym}."""\n'
            f"    result = {sym}()\n"
            f"    assert result is not None, 'issue not reproduced / fixed'\n"
        )
    elif kind == "security_regression":
        body = (
            f"def {func}():\n"
            f'    """Security regression test for {sym} in {source_path}."""\n'
            f'    malicious = "\'; DROP TABLE users; --"\n'
            f"    outcome = {sym}(malicious)\n"
            f"    assert outcome != malicious, 'input must be sanitized'\n"
        )
    else:
        body = (
            f"def {func}():\n"
            f'    """Regression test for {sym} in {source_path}."""\n'
            f"    result = {sym}()\n"
            f"    assert result is not None, 'expected a result'\n"
        )
    return "import pytest\n\n" + body


def propose_test(
    *,
    repo_root: str | Path,
    source_path: str,
    kind: str = "regression",
    target_symbol: str = "",
    test_path: str | None = None,
    issue_text: str = "",
    finding: object | None = None,
    intended_to_fail_before_fix: bool | None = None,
) -> TestProposal:
    """Generate a :class:`TestProposal` for the given source file.

    The generated test code is stored in ``metadata["test_code"]`` so the verifier
    can statically check it is non-trivial. ``verification_status`` starts as
    ``"proposed"`` because nothing is executed offline.
    """
    stem = Path(source_path).stem
    test_path = test_path or f"tests/test_{stem}.py"
    proposal_id = f"test:{kind}:{source_path}"
    if intended_to_fail_before_fix is None:
        intended_to_fail_before_fix = True
    note = ""
    if kind == "repro" and issue_text:
        note = issue_text.strip().splitlines()[0] if issue_text.strip() else ""
    elif finding is not None:
        note = getattr(finding, "summary", "") or getattr(finding, "title", "")
    test_code = _test_code(
        kind=kind,
        test_path=test_path,
        target_symbol=target_symbol or stem,
        source_path=source_path,
        note=note,
    )
    finding_id = getattr(finding, "finding_id", "") if finding is not None else ""
    return TestProposal(
        proposal_id=proposal_id,
        kind=ActionKind.TEST,
        finding_ids=[finding_id] if finding_id else [],
        title=f"{kind} test for {source_path}",
        description=note,
        test_paths=[test_path],
        command=f"pytest {test_path}",
        intended_to_fail_before_fix=intended_to_fail_before_fix,
        metadata={
            "test_code": test_code,
            "verification_status": "proposed",
            "kind": kind,
            "target_symbol": target_symbol or stem,
        },
    )


def generate_repro_test(
    *,
    repo_root: str | Path,
    source_path: str,
    issue_text: str,
    target_symbol: str = "",
) -> TestProposal:
    """Generate a reproduction test for an issue (fails before the fix)."""
    return propose_test(
        repo_root=repo_root,
        source_path=source_path,
        kind="repro",
        target_symbol=target_symbol,
        issue_text=issue_text,
        intended_to_fail_before_fix=True,
    )


def generate_security_regression_test(
    *,
    repo_root: str | Path,
    source_path: str,
    finding: object,
    target_symbol: str = "",
) -> TestProposal:
    """Generate a security regression test from a security finding."""
    return propose_test(
        repo_root=repo_root,
        source_path=source_path,
        kind="security_regression",
        target_symbol=target_symbol,
        finding=finding,
        intended_to_fail_before_fix=True,
    )
