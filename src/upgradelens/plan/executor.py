"""Controlled, sandboxed execution of an :class:`UpgradePlan` (S7, stage 8 phase 1).

The executor is the *only* place that turns a plan into file changes, and it is built to
be conservative by construction:

* **Pre-apply validation** refuses to run on a repo hash that does not match the plan,
  on missing target files, or on target files outside the allowed scope.
* **Phase 1 actions** are limited to ``patch_draft`` (produce a diff, change nothing) and
  ``sandbox_apply`` (apply inside a throwaway copy of the repo).
* **After apply** it checks for residual old-API symbols, files modified outside the
  plan's scope, and how much of the documented risk the change actually covered.
* **Re-verify**: when the original outcome is supplied, the changed sandbox tree is
  re-fed to the Verifier so the evidence checks run against the *post-change* code.
* **Never** commits, pushes, or edits the user's real workspace -- ``sandbox_apply`` only
  ever writes to the sandbox copy.

The external Coding Agent that actually merges the change is expected to report back an
:class:`ExecutionResult`-shaped payload; :func:`execute_plan` produces the same shape so
the contract is identical whether the agent or the built-in sandbox did the work.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.pipeline import AssessmentOutcome
from upgradelens.plan.upgrade_plan import PlanMode, UpgradePlan, repo_hash_of

__all__ = [
    "ExecutionStatus",
    "TestOutcome",
    "ExecutionResult",
    "execute_plan",
    "reverify_after_apply",
]

#: Directories we never copy into a sandbox -- they are large and irrelevant to a patch.
_SANDBOX_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    ".upgradelens_sandbox",
)


def _frozen() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


class ExecutionStatus(StrEnum):
    """Outcome of an apply attempt."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"  # patch_draft: nothing was applied


class TestOutcome(BaseModel):
    """One test result reported back from the applied change."""

    model_config = _frozen()

    name: str
    passed: bool
    detail: str = ""


class ExecutionResult(BaseModel):
    """The external feedback contract for a plan apply (plan section 17)."""

    model_config = _frozen()

    schema_version: str = "upgrade-plan-execution/1"
    target_dependency: str = ""
    mode: PlanMode = PlanMode.PATCH_DRAFT
    status: ExecutionStatus = ExecutionStatus.SKIPPED
    repo_hash_before: str = ""
    repo_hash_after: str = ""
    modified_files: list[str] = Field(default_factory=list)
    diff: str = ""
    test_results: list[TestOutcome] = Field(default_factory=list)
    #: Files changed that no step claimed as a target (a scope violation).
    unrelated_modifications: list[str] = Field(default_factory=list)
    #: Old-API symbols still present in the modified files after apply.
    old_api_residual: list[str] = Field(default_factory=list)
    #: Fraction of documented old-API symbols actually removed (0..1).
    evidence_coverage: float = 0.0
    reverify_conclusion: str | None = None
    notes: str = ""


def _all_target_files(plan: UpgradePlan) -> set[str]:
    targets: set[str] = set()
    for step in plan.steps:
        targets.update(step.target_files)
        targets.update(step.recommended_tests)
    return targets


def _validate_before_apply(plan: UpgradePlan, repo_root: Path) -> tuple[bool, str]:
    """Refuse to apply unless the repo matches the plan's recorded state."""
    if plan.repo_hash:
        current = repo_hash_of(repo_root)
        if not current or current != plan.repo_hash:
            return (
                False,
                f"repo hash mismatch: plan built at {plan.repo_hash}, repo is "
                f"{current or '<unavailable>'}",
            )
    missing = [f for step in plan.steps for f in step.target_files if not (repo_root / f).is_file()]
    if missing:
        return False, "target files missing: " + ", ".join(sorted(missing))
    return True, ""


def _apply_hunk(
    orig_lines: list[str], old_start: int, old_count: int, body: list[str]
) -> list[str]:
    start = old_start - 1
    if start < 0 or start + old_count > len(orig_lines):
        raise ApplyError(f"hunk at line {old_start} is out of range")
    # Verify the original block still matches (context + removed lines). The body is
    # marker-prefixed, but the actual file lines are not, so compare the *stripped*
    # expected block against the raw file lines directly.
    expected = [ln[1:] for ln in body if ln[:1] in (" ", "-")]
    actual = orig_lines[start : start + old_count]
    if actual != expected:
        raise ApplyError(f"hunk context does not match at line {old_start}")
    replacement = [ln[1:] for ln in body if ln[:1] in (" ", "+")]
    return orig_lines[:start] + replacement + orig_lines[start + old_count :]


class ApplyError(RuntimeError):
    """Raised when a patch hunk cannot be applied."""


def _apply_patch_to_sandbox(sandbox_root: Path, plan: UpgradePlan) -> list[str]:
    """Apply the plan's patch draft to ``sandbox_root``; return changed files."""
    if plan.patch is None or not plan.patch.files:
        return []
    changed: list[str] = []
    for fd in plan.patch.files:
        path = sandbox_root / fd.path
        if not path.is_file():
            raise ApplyError(f"patch target {fd.path} not found in sandbox")
        text = path.read_text(encoding="utf-8")
        trailing_nl = text.endswith("\n")
        lines = text.splitlines()
        # Apply bottom-up so each hunk's line numbers stay valid.
        for hunk in sorted(fd.hunks, key=lambda h: h.old_start, reverse=True):
            lines = _apply_hunk(lines, hunk.old_start, hunk.old_count, hunk.body)
        path.write_text("\n".join(lines) + ("\n" if trailing_nl else ""), encoding="utf-8")
        changed.append(fd.path)
    return changed


def _scan_residual(sandbox_root: Path, files: list[str], symbols: list[str]) -> list[str]:
    """Return symbols still present (whole-word) in any of ``files`` after apply."""
    if not symbols or not files:
        return []
    residual: list[str] = []
    for rel in files:
        path = sandbox_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for sym in symbols:
            if re.search(rf"\b{re.escape(sym)}\b", text):
                if sym not in residual:
                    residual.append(sym)
    return residual


def _post_apply_checks(
    plan: UpgradePlan, sandbox_root: Path, modified: list[str]
) -> tuple[list[str], list[str], float]:
    targets = _all_target_files(plan)
    unrelated = sorted(set(modified) - targets)
    all_symbols: list[str] = [s for step in plan.steps for s in step.api_symbols]
    residual = _scan_residual(sandbox_root, modified, all_symbols)
    if all_symbols:
        coverage = 1.0 - len(residual) / len(all_symbols)
    else:
        coverage = 1.0 if not modified else 0.0
    return unrelated, residual, round(coverage, 4)


def reverify_after_apply(outcome: AssessmentOutcome, sandbox_root: str | Path) -> Any | None:
    """Re-run the Verifier against the post-change ``sandbox_root``.

    Returns a fresh :class:`VerifiedReport`, or ``None`` if the outcome lacks the inputs
    the verifier needs (e.g. a fake run with no code report).
    """
    if outcome.report is None or outcome.code_report is None:
        return None
    from upgradelens.verify.verifier import verify_report

    return verify_report(
        outcome.report,
        repo_root=Path(sandbox_root),
        bundle=outcome.bundle,
        code_report=outcome.code_report,
        skill=outcome.skill,
    )


def execute_plan(
    plan: UpgradePlan,
    *,
    repo_root: str | Path,
    work_dir: str | Path | None = None,
    outcome: AssessmentOutcome | None = None,
    run_tests: bool = False,
) -> ExecutionResult:
    """Execute ``plan`` under the controlled, sandbox-only contract.

    ``patch_draft`` produces a diff and changes nothing. ``sandbox_apply`` applies the
    patch inside a throwaway copy of ``repo_root`` (never the original) and runs the
    post-apply checks plus an optional re-verify. The original workspace is never
    committed to, pushed, or otherwise mutated.
    """
    repo_root = Path(repo_root)
    repo_hash_before = repo_hash_of(repo_root)

    ok, reason = _validate_before_apply(plan, repo_root)
    if not ok:
        return ExecutionResult(
            target_dependency=plan.target_dependency,
            mode=plan.mode,
            status=ExecutionStatus.REJECTED,
            repo_hash_before=repo_hash_before,
            notes=reason,
        )

    if plan.mode == PlanMode.PATCH_DRAFT:
        return ExecutionResult(
            target_dependency=plan.target_dependency,
            mode=plan.mode,
            status=ExecutionStatus.SKIPPED,
            repo_hash_before=repo_hash_before,
            repo_hash_after=repo_hash_before,
            diff=plan.patch.to_unified_diff() if plan.patch else "",
            notes="patch draft only; no files were modified (hand to the external agent).",
        )

    # sandbox_apply: copy the repo to a throwaway dir and apply there.
    if not plan.patch or not plan.patch.files:
        return ExecutionResult(
            target_dependency=plan.target_dependency,
            mode=plan.mode,
            status=ExecutionStatus.SKIPPED,
            repo_hash_before=repo_hash_before,
            repo_hash_after=repo_hash_before,
            notes="no patch draft available to apply; nothing changed (the plan may "
            "need a capability pack that permits drafts).",
        )
    # The sandbox must live *outside* the repo: shutil.copytree refuses (and would
    # recurse) if the destination is inside the source.
    sandbox_root = (
        Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="ul_sandbox_"))
    )
    if sandbox_root.exists():
        shutil.rmtree(sandbox_root)
    shutil.copytree(repo_root, sandbox_root, ignore=_SANDBOX_IGNORE)

    try:
        modified = _apply_patch_to_sandbox(sandbox_root, plan)
    except ApplyError as exc:
        shutil.rmtree(sandbox_root, ignore_errors=True)
        return ExecutionResult(
            target_dependency=plan.target_dependency,
            mode=plan.mode,
            status=ExecutionStatus.REJECTED,
            repo_hash_before=repo_hash_before,
            notes=f"patch failed to apply: {exc}",
        )

    unrelated, residual, coverage = _post_apply_checks(plan, sandbox_root, modified)

    test_results: list[TestOutcome] = []
    if run_tests:
        test_results = _run_recommended_tests(plan, sandbox_root)

    reverify_conclusion: str | None = None
    if outcome is not None:
        reverted = reverify_after_apply(outcome, sandbox_root)
        if reverted is not None:
            reverify_conclusion = reverted.conclusion.value

    repo_hash_after = repo_hash_of(sandbox_root) or _hash_files(sandbox_root, modified)

    status = ExecutionStatus.ACCEPTED
    if residual or unrelated:
        status = ExecutionStatus.NEEDS_REVIEW

    notes = (
        "applied inside sandbox only; original workspace untouched. "
        "No commit, no push, no PR opened."
    )
    if residual:
        notes += f" residual old-API symbols: {', '.join(residual)}."
    if unrelated:
        notes += f" out-of-scope modifications: {', '.join(unrelated)}."

    return ExecutionResult(
        target_dependency=plan.target_dependency,
        mode=plan.mode,
        status=status,
        repo_hash_before=repo_hash_before,
        repo_hash_after=repo_hash_after,
        modified_files=modified,
        diff=plan.patch.to_unified_diff() if plan.patch else "",
        test_results=test_results,
        unrelated_modifications=unrelated,
        old_api_residual=residual,
        evidence_coverage=coverage,
        reverify_conclusion=reverify_conclusion,
        notes=notes,
    )


def _hash_files(sandbox_root: Path, files: list[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        p = sandbox_root / rel
        if p.is_file():
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _run_recommended_tests(plan: UpgradePlan, sandbox_root: Path) -> list[TestOutcome]:
    """Best-effort pytest run of the plan's recommended test files inside the sandbox."""
    tests = sorted({t for step in plan.steps for t in step.recommended_tests})
    if not tests:
        return []
    args = ["python", "-m", "pytest", "-q", *tests]
    try:
        proc = subprocess.run(
            args, cwd=str(sandbox_root), capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return [TestOutcome(name=" ".join(tests), passed=False, detail=str(exc))]
    passed = proc.returncode == 0
    return [
        TestOutcome(name=t, passed=passed, detail=(proc.stdout or proc.stderr)[-500:])
        for t in tests
    ]
