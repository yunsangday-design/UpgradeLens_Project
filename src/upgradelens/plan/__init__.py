"""S7: the externally-consumable upgrade plan + controlled-execution contract.

Stage 8 of the plan produces an audited :class:`~upgradelens.verify.models.VerifiedReport`.
That report is for *human* reading. An external Coding Agent (or a human applying the
change by hand) needs something more actionable: a stable, machine-readable
:class:`UpgradePlan` whose every step names the target files, the API symbols to remove,
why, which docs back it, a suggested approach, what must *not* be touched, which tests
to run, and how to know the step is done.

The plan is deliberately paired with an :class:`ExecutionResult` feedback contract: after
the external agent applies the change it reports back the modified files, the diff, the
test results and the repo hash. :mod:`upgradelens.plan.executor` turns that contract into
the controlled, sandbox-only apply described in the plan (phase 1: *patch draft* or
*sandbox apply* -- never auto-commit, never auto-push, never mutates the user workspace
unless a sandbox copy is explicitly requested).
"""

from __future__ import annotations

from upgradelens.plan.executor import (
    ExecutionResult,
    ExecutionStatus,
    TestOutcome,
    execute_plan,
    reverify_after_apply,
)
from upgradelens.plan.upgrade_plan import (
    PlanMode,
    UpgradePlan,
    UpgradeStep,
    build_upgrade_plan,
    export_plan,
    repo_hash_of,
)

__all__ = [
    "ExecutionResult",
    "ExecutionStatus",
    "PlanMode",
    "TestOutcome",
    "UpgradePlan",
    "UpgradeStep",
    "build_upgrade_plan",
    "export_plan",
    "execute_plan",
    "reverify_after_apply",
    "repo_hash_of",
]
