"""Test Intelligence (plan stage S8).

A horizontal capability package (not a standalone Capability) that the
dependency-upgrade, PR-review, issue-repair and security-review capabilities reuse
to recommend, gap-analyze, propose and verify tests.
"""

from __future__ import annotations

from .gap import TestGap, TestGapKind, analyze_test_gaps
from .profile import PytestConfig, TestProfileInfo, build_test_profile
from .proposal import (
    TestProposalSpec,
    generate_repro_test,
    generate_security_regression_test,
    propose_test,
)
from .selector import TestSelection, recommend_regression_tests, select_tests
from .verifier import ASSERTION_RE, TRIVIAL_ASSERT_RE, verify_test_proposal

__all__ = [
    "PytestConfig",
    "TestProfileInfo",
    "build_test_profile",
    "TestSelection",
    "select_tests",
    "recommend_regression_tests",
    "TestGap",
    "TestGapKind",
    "analyze_test_gaps",
    "TestProposalSpec",
    "propose_test",
    "generate_repro_test",
    "generate_security_regression_test",
    "ASSERTION_RE",
    "TRIVIAL_ASSERT_RE",
    "verify_test_proposal",
]
