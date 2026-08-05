"""Load Skill Packs from YAML on disk (plan sections 8.9-8.12, 3).

A Skill Pack directory contains ``skill.yaml`` (metadata/descriptor) plus
optional sibling files: ``patterns.yaml``, ``sources.yaml`` and
``patch_rules.yaml``. The loader merges them into a single
:class:`~upgradelens.domain.skill.SkillPackage` and validates version
specifiers using :mod:`packaging`.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ValidationError

from upgradelens.domain.skill import (
    DocSource,
    PatchRule,
    SkillPackage,
    UsagePattern,
)

SKILL_FILE = "skill.yaml"
PATTERNS_FILE = "patterns.yaml"
SOURCES_FILE = "sources.yaml"
PATCH_RULES_FILE = "patch_rules.yaml"

# Files that contribute to the content hash, in a fixed order so the hash is
# deterministic regardless of directory iteration order.
_HASHED_FILES = (SKILL_FILE, PATTERNS_FILE, SOURCES_FILE, PATCH_RULES_FILE)


class SkillParseError(Exception):
    """Raised when a Skill Pack directory cannot be parsed into a valid skill."""


def _read_yaml(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise SkillParseError(f"{path}: invalid YAML: {exc}") from exc


def _validate_specifier(raw: str | None, where: str) -> None:
    if raw is None:
        return
    try:
        SpecifierSet(str(raw))
    except InvalidSpecifier as exc:
        raise SkillParseError(f"{where}: invalid PEP 440 version specifier {raw!r}: {exc}") from exc


def _content_hash(skill_dir: Path) -> str:
    from hashlib import sha256

    digest = sha256()
    for name in _HASHED_FILES:
        path = skill_dir / name
        if path.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _parse_list[T: BaseModel](data: object, model: type[T], path: Path, label: str) -> list[T]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise SkillParseError(f"{path}: {label} must be a YAML list")
    try:
        return [model.model_validate(item) for item in data]
    except ValidationError as exc:
        raise SkillParseError(f"{path}: {exc}") from exc


def _load_sibling[T: BaseModel](path: Path, model: type[T], label: str) -> list[T] | None:
    if not path.is_file():
        return None
    return _parse_list(_read_yaml(path), model, path, label)


def load_skill_package(skill_dir: str | Path) -> SkillPackage:
    """Load and validate one Skill Pack from ``skill_dir``."""
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        raise SkillParseError(f"skill directory not found: {skill_dir}")

    meta_path = skill_dir / SKILL_FILE
    if not meta_path.is_file():
        raise SkillParseError(f"missing {SKILL_FILE} in {skill_dir}")

    raw_meta = _read_yaml(meta_path)
    if not isinstance(raw_meta, dict):
        raise SkillParseError(f"{meta_path}: {SKILL_FILE} must be a mapping")

    try:
        skill = SkillPackage.model_validate(raw_meta)
    except ValidationError as exc:
        raise SkillParseError(f"{meta_path}: {exc}") from exc

    patterns = _load_sibling(skill_dir / PATTERNS_FILE, UsagePattern, "patterns")
    if patterns is not None:
        skill.patterns = patterns

    sources = _load_sibling(skill_dir / SOURCES_FILE, DocSource, "sources")
    if sources is not None:
        skill.sources = sources

    patch_rules = _load_sibling(skill_dir / PATCH_RULES_FILE, PatchRule, "patch_rules")
    if patch_rules is not None:
        skill.patch_rules = patch_rules

    # Validate version specifiers *after* the merge so sibling files can carry
    # overriding specifiers if present.
    _validate_specifier(skill.source_version_spec, f"{skill.skill_id}.source_version_spec")
    _validate_specifier(skill.target_version_spec, f"{skill.skill_id}.target_version_spec")
    for src in skill.sources:
        _validate_specifier(src.target_version_spec, f"{skill.skill_id}.sources.{src.id}")

    skill.content_hash = _content_hash(skill_dir)
    skill.source_path = str(skill_dir)
    return skill


def discover_skills(base_dir: str | Path) -> list[SkillPackage]:
    """Load every valid Skill Pack found directly under ``base_dir``."""
    base_dir = Path(base_dir)
    skills: list[SkillPackage] = []
    if not base_dir.is_dir():
        return skills
    for child in sorted(base_dir.iterdir()):
        if child.is_dir() and (child / SKILL_FILE).is_file():
            skills.append(load_skill_package(child))
    return skills
