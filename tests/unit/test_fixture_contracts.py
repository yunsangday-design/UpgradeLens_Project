"""End-to-end contract tests driven by the fixtures (plan sections 8.1-8.2).

Each fixture directory is a self-contained contract: ``request.json`` in,
``expected_dependency_scan.json`` out. Adding a fixture directory automatically
adds a test case, and the expected file is the single source of truth — the
assertion compares the whole document, so an accidental extra or renamed field
fails loudly instead of silently changing the output shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upgradelens.analyzers import scan_repository

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_DIRS = sorted(path for path in FIXTURES_ROOT.iterdir() if path.is_dir())

# Stage 2 code-evidence fixtures (e.g. pydantic_usage) use a different contract
# file (no expected_dependency_scan.json); keep them out of the stage 1 checks.
STAGE2_CODE_FIXTURES = {"pydantic_usage"}
# Stage 6 evaluation cases live under a container directory: `eval/` is not a
# fixture itself, it holds one sub-directory per case with its own case.yaml
# contract (see tests/unit/test_eval_harness.py).
EVAL_CONTAINERS = {"eval"}
# Record-replay capture directories (e.g. `llm_replay/`) are containers too:
# they hold recorded LLM request/response pairs, not stage 1 scan contracts.
REPLAY_CONTAINERS = {"llm_replay"}
# Retrieval evaluation cases (ROADMAP Step 4, B0) live under `retrieval/`: each
# file is a labelled `(pattern, code_symbols) -> expected_chunks` case consumed by
# `upgradelens eval retrieval-baseline`, not a stage 1 scan contract.
# `retrieval_shared/` is its S6 counterpart (shared corpus, no curated queries),
# and `corpus/` holds the source manifests plus snapshots those cases retrieve
# from -- documentation data, not repositories to scan.
RETRIEVAL_CONTAINERS = {"retrieval", "retrieval_shared", "corpus"}
NON_STAGE1 = STAGE2_CODE_FIXTURES | EVAL_CONTAINERS | REPLAY_CONTAINERS | RETRIEVAL_CONTAINERS
STAGE1_FIXTURE_DIRS = [p for p in FIXTURE_DIRS if p.name not in NON_STAGE1]
STAGE1_FIXTURE_IDS = [p.name for p in STAGE1_FIXTURE_DIRS]


def _fixture_ids() -> list[str]:
    return [path.name for path in FIXTURE_DIRS]


def test_fixture_set_is_complete() -> None:
    assert _fixture_ids() == [
        "corpus",
        "eval",
        "llm_replay",
        "pydantic_config",
        "pydantic_root_validator",
        "pydantic_serialization",
        "pydantic_usage",
        "pydantic_validator",
        "retrieval",
        "retrieval_shared",
    ]


@pytest.mark.parametrize("fixture_dir", STAGE1_FIXTURE_DIRS, ids=STAGE1_FIXTURE_IDS)
def test_fixture_matches_expected_contract(fixture_dir: Path) -> None:
    request_data = json.loads((fixture_dir / "request.json").read_text(encoding="utf-8"))
    expected = json.loads(
        (fixture_dir / "expected_dependency_scan.json").read_text(encoding="utf-8")
    )

    manifest_path = request_data["manifest_path"]
    result = scan_repository(
        repository_root=fixture_dir / request_data["repository_root"],
        dependency_name=request_data["dependency_name"],
        target_version=request_data["target_version"],
        manifest_path=Path(manifest_path) if manifest_path else None,
    )

    assert result.model_dump(mode="json") == expected


@pytest.mark.parametrize("fixture_dir", STAGE1_FIXTURE_DIRS, ids=STAGE1_FIXTURE_IDS)
def test_fixture_paths_are_posix_relative(fixture_dir: Path) -> None:
    expected = json.loads(
        (fixture_dir / "expected_dependency_scan.json").read_text(encoding="utf-8")
    )
    for declaration in expected["declarations"]:
        path = declaration["path"]
        assert "\\" not in path
        assert not path.startswith("/")
        assert ":" not in path


@pytest.mark.parametrize("fixture_dir", FIXTURE_DIRS, ids=_fixture_ids())
def test_fixture_has_readme(fixture_dir: Path) -> None:
    readme = fixture_dir / "README.md"
    assert readme.is_file()
    assert readme.read_text(encoding="utf-8").strip()
