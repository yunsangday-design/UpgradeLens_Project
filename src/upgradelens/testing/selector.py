"""Test Selection: recommend existing tests for changed/impacted symbols (S8)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.change.impact import ChangeImpact
from upgradelens.change.models import ChangeLabel, ChangeSet
from upgradelens.repository.models import RepositoryProfile

__all__ = ["TestSelection", "select_tests", "recommend_regression_tests"]


class TestSelection(BaseModel):
    """A recommendation of which tests to run for a changed source file."""

    model_config = ConfigDict(frozen=True)

    source_path: str
    selected_tests: list[str] = Field(default_factory=list)
    is_new: bool = False
    proposal_id: str = ""


def _match_tests(change_path: str, existing: list[str]) -> list[str]:
    stem = Path(change_path).stem
    return [
        t
        for t in existing
        if f"test_{stem}" in Path(t).name or f"{stem}_test" in Path(t).name or stem in Path(t).stem
    ]


def select_tests(
    change_set: ChangeSet,
    impact: ChangeImpact,
    profile: RepositoryProfile,
) -> list[TestSelection]:
    """Map each changed python file to existing tests, or propose a new test.

    A changed ``foo.py`` recommends the matching ``test_foo.py`` / ``foo_test.py``
    when present; otherwise it proposes a new ``tests/test_foo.py`` (``is_new``).
    """
    existing = list(profile.tests.test_paths)
    selections: list[TestSelection] = []
    for change in change_set.files:
        if not change.path.endswith(".py"):
            continue
        if change.label is ChangeLabel.DELETED:
            continue
        matched = _match_tests(change.path, existing)
        is_new = not matched
        if is_new:
            matched = [f"tests/test_{Path(change.path).stem}.py"]
        selections.append(
            TestSelection(
                source_path=change.path,
                selected_tests=matched,
                is_new=is_new,
                proposal_id=f"test:{change.path}",
            )
        )
    return selections


def recommend_regression_tests(
    change_set: ChangeSet,
    impact: ChangeImpact,
    profile: RepositoryProfile,
) -> list[str]:
    """Return a deduplicated list of recommended regression test paths."""
    out: list[str] = []
    for sel in select_tests(change_set, impact, profile):
        for t in sel.selected_tests:
            if t not in out:
                out.append(t)
    return out
