"""Every front door must answer with the same assessment.

The CLI, the MCP server and the Streamlit demo each used to re-derive the
assessment sequence by hand, and they drifted: different Skill Pack resolution,
differently worded degradations, and a checkout that the CLI deleted before
verifying against it. They now share :mod:`upgradelens.pipeline`; these tests
are the tripwire that keeps them there.

The comparison is on the *verified* report rather than the raw
:class:`~upgradelens.models.impact.ImpactReport`, which is the stricter check:
verification is derived from the report plus the evidence and the repository, so
two identical verified reports imply identical reports *and* identical inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from demo.pipeline import run_assess
from upgradelens.cli import EXIT_OK, main
from upgradelens.pipeline import AssessmentRequest, collect_evidence
from upgradelens.tools.registry import ToolContext

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pydantic_usage"
DEPENDENCY = "pydantic"
TARGET = "2.0"

#: Wall-clock stamps differ between two runs by construction.
_VOLATILE = frozenset({"generated_at"})


def _stable(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key not in _VOLATILE}


def test_cli_and_mcp_return_the_same_verified_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mcp_server = pytest.importorskip("upgradelens.mcp.server")

    code = main(
        [
            "assess",
            "--repo",
            str(FIXTURE),
            "--dependency",
            DEPENDENCY,
            "--target-version",
            TARGET,
            "--mode",
            "fake",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    from_cli = json.loads(captured.out)

    from_mcp = mcp_server.assess(
        repo=str(FIXTURE),
        dependency=DEPENDENCY,
        target_version=TARGET,
        mode="fake",
    )

    assert _stable(from_cli) == _stable(from_mcp)


def test_demo_reuses_the_shared_evidence_collection() -> None:
    """The demo may fake the *model*, never the evidence.

    Its canned responses are the whole point of an offline walkthrough, so the
    reports legitimately differ. What must not differ is anything the pipeline
    derived: the code scan, the resolved Skill Pack, and the caveats attached to
    the run.
    """
    request = AssessmentRequest(repo=str(FIXTURE), dependency=DEPENDENCY, target_version=TARGET)
    with ToolContext() as ctx:
        expected = collect_evidence(request, ctx)

    result = run_assess(
        repo=str(FIXTURE),
        dependency=DEPENDENCY,
        target_version=TARGET,
        mode="fake",
        model="",
        api_key="",
        base_url="",
        allow_quality_patch=False,
    )

    assert result["code_report"] == expected.code_report
    assert expected.skill is not None
    assert result["skill"] is not None
    assert result["skill"].skill_id == expected.skill.skill_id
    # The verifier appends caveats of its own, so this is containment rather
    # than equality: every caveat the *pipeline* raised must reach the demo.
    assert set(expected.degradations) <= set(result["verified"].degradations)
