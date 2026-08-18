"""Unit tests for the stage 3 Skill Pack YAML loader.

These tests build tiny Skill Pack directories in ``tmp_path`` and assert the
loader behaves on the cases called out by the stage 3 plan: valid packs merge
their sibling YAML files, invalid packs raise :class:`SkillParseError`, and the
content hash is stable yet sensitive to file contents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.skills import SkillParseError, load_skill_package

DEMO_SKILL = (
    "skill_id: demo\n"
    "package_names:\n  - demo\n"
    "target_version_spec: '>=2,<3'\n"
    "priority: 10\n"
    "support_status: dedicated\n"
    "version: '1.0.0'\n"
)
DEMO_PATTERNS = "- id: p1\n  kind: method_call\n  match: demo\n"
DEMO_SOURCES = "- id: s1\n  url: https://example.com/doc\n"
DEMO_PATCH = "- id: r1\n  target_pattern: a\n  replacement_template: b\n"


def _write_skill(
    root: Path,
    *,
    skill: str = DEMO_SKILL,
    patterns: str | None = DEMO_PATTERNS,
    sources: str | None = DEMO_SOURCES,
    patch_rules: str | None = DEMO_PATCH,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skill.yaml").write_text(skill, encoding="utf-8")
    if patterns is not None:
        (root / "patterns.yaml").write_text(patterns, encoding="utf-8")
    if sources is not None:
        (root / "sources.yaml").write_text(sources, encoding="utf-8")
    if patch_rules is not None:
        (root / "patch_rules.yaml").write_text(patch_rules, encoding="utf-8")
    return root


def test_valid_dedicated_skill_merges_siblings(tmp_path: Path) -> None:
    skill = load_skill_package(_write_skill(tmp_path / "demo"))

    assert skill.skill_id == "demo"
    assert skill.support_status == "dedicated"
    assert skill.priority == 10
    assert len(skill.patterns) == 1
    assert skill.patterns[0].id == "p1"
    assert len(skill.sources) == 1
    assert skill.sources[0].url == "https://example.com/doc"
    assert len(skill.patch_rules) == 1
    assert skill.patch_rules[0].target_pattern == "a"
    assert skill.content_hash  # non-empty after load


def test_missing_skill_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(SkillParseError, match="missing SKILL.md or skill.yaml"):
        load_skill_package(tmp_path / "empty")


def test_invalid_missing_skill_id_raises(tmp_path: Path) -> None:
    _write_skill(tmp_path / "demo", skill="package_names:\n  - demo\n")
    with pytest.raises(SkillParseError):
        load_skill_package(tmp_path / "demo")


def test_invalid_version_spec_raises(tmp_path: Path) -> None:
    bad = DEMO_SKILL.replace("'>=2,<3'", "not-a-spec")
    _write_skill(tmp_path / "demo", skill=bad)
    with pytest.raises(SkillParseError, match="version specifier"):
        load_skill_package(tmp_path / "demo")


def test_invalid_enum_value_raises(tmp_path: Path) -> None:
    bad = DEMO_SKILL.replace("dedicated", "beta")
    _write_skill(tmp_path / "demo", skill=bad)
    with pytest.raises(SkillParseError):
        load_skill_package(tmp_path / "demo")


def test_optional_sibling_files_default_to_empty(tmp_path: Path) -> None:
    skill = load_skill_package(
        _write_skill(tmp_path / "gen", patterns=None, sources=None, patch_rules=None)
    )
    assert skill.patterns == []
    assert skill.sources == []
    assert skill.patch_rules == []


def test_content_hash_is_stable_and_sensitive(tmp_path: Path) -> None:
    first = load_skill_package(_write_skill(tmp_path / "a"))
    second = load_skill_package(_write_skill(tmp_path / "b"))
    assert first.content_hash == second.content_hash

    changed = load_skill_package(
        _write_skill(
            tmp_path / "c",
            patterns=DEMO_PATTERNS
            + "- id: p2\n  kind: call\n  match: x\n".replace("kind: call", "kind: method_call"),
        )
    )
    assert changed.content_hash != first.content_hash
