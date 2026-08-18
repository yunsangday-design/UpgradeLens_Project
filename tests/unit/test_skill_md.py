"""Tests for the SKILL.md Skill Pack format (frontmatter + markdown body)."""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.skills import SKILL_MD_FILE, discover_builtin_skills
from upgradelens.skills.loader import (
    SkillParseError,
    discover_skills,
    load_skill_package,
)

_MD_PACK = """---
skill_id: md_demo
name: MD Demo Pack
package_names:
  - demo
source_version_spec: ">=1,<2"
target_version_spec: ">=2,<3"
priority: 50
support_status: dedicated
risk_categories:
  - api
allow_patch_draft: true
version: "1.0.0"
---

# MD Demo Pack

Lead paragraph describing the pack: covers demo 1.x to 2.x breakage.

More description continues here.

## Limitations

Only the first batch of statically detectable patterns.
"""


def _write_pack(tmp_path: Path, name: str = "md_demo") -> Path:
    pack = tmp_path / name
    pack.mkdir()
    (pack / SKILL_MD_FILE).write_text(_MD_PACK, encoding="utf-8")
    return pack


def test_skill_md_frontmatter_and_body(tmp_path: Path) -> None:
    skill = load_skill_package(_write_pack(tmp_path))
    assert skill.skill_id == "md_demo"
    assert skill.priority == 50
    assert skill.package_names == ["demo"]
    # description comes from the lead paragraph (after the H1), joined.
    assert "Lead paragraph" in skill.description
    assert "More description continues" in skill.description
    # limitations comes from the ## Limitations section.
    assert "first batch" in skill.limitations


def test_skill_md_takes_precedence_over_yaml(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path)
    (pack / "skill.yaml").write_text(
        "skill_id: legacy\nname: Legacy\npackage_names: ['*']\n"
        "priority: 1\nsupport_status: generic\nrisk_categories: []\n"
        "allow_patch_draft: false\nversion: '0.0.1'\n",
        encoding="utf-8",
    )
    skill = load_skill_package(pack)
    assert skill.skill_id == "md_demo"
    assert skill.priority == 50


def test_legacy_yaml_still_loads(tmp_path: Path) -> None:
    pack = tmp_path / "legacy_pack"
    pack.mkdir()
    (pack / "skill.yaml").write_text(
        "skill_id: legacy\nname: Legacy\npackage_names: ['*']\n"
        "priority: 1\nsupport_status: generic\nrisk_categories: []\n"
        "allow_patch_draft: false\nversion: '0.0.1'\n",
        encoding="utf-8",
    )
    skill = load_skill_package(pack)
    assert skill.skill_id == "legacy"
    assert skill.is_generic


def test_missing_descriptor_raises(tmp_path: Path) -> None:
    pack = tmp_path / "empty"
    pack.mkdir()
    with pytest.raises(SkillParseError):
        load_skill_package(pack)


def test_unterminated_frontmatter_raises(tmp_path: Path) -> None:
    pack = tmp_path / "broken"
    pack.mkdir()
    (pack / SKILL_MD_FILE).write_text("---\nskill_id: broken\nname: Broken\n", encoding="utf-8")
    with pytest.raises(SkillParseError):
        load_skill_package(pack)


def test_builtin_packs_load_from_skill_md() -> None:
    skills = {s.skill_id: s for s in discover_builtin_skills()}
    assert set(skills) == {
        "generic_python_dependency",
        "pydantic_v1_to_v2",
        "sqlalchemy_v1_to_v2",
    }
    # Bodies carried over: descriptions are non-empty prose from the markdown.
    for skill in skills.values():
        assert skill.description
        assert skill.limitations
    # Version specifiers survive the migration.
    assert skills["pydantic_v1_to_v2"].source_version_spec == ">=1,<2"
    assert skills["sqlalchemy_v1_to_v2"].source_version_spec == ">=1.4,<2"


def test_discover_skills_finds_md_packs(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    found = discover_skills(tmp_path)
    assert [s.skill_id for s in found] == ["md_demo"]
