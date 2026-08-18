"""Dependency Upgrade test-intelligence wiring (plan stage S8).

Horizontal capability hook: a dependency upgrade produces a change set, so the
same test-intelligence selection logic recommends regression tests to run after
the upgrade.
"""

from __future__ import annotations

from upgradelens.change.impact import ChangeImpact
from upgradelens.change.models import ChangeSet
from upgradelens.repository.models import RepositoryProfile
from upgradelens.testing import recommend_regression_tests as _recommend_regression_tests

__all__ = ["recommend_regression_tests"]


def recommend_regression_tests(
    change_set: ChangeSet, impact: ChangeImpact, profile: RepositoryProfile
) -> list[str]:
    """Recommend regression tests to run after a dependency upgrade (S8)."""
    return _recommend_regression_tests(change_set, impact, profile)
