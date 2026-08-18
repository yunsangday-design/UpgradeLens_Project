"""Load Skill Packs from disk (plan sections 8.9-8.12, 3).

A Skill Pack directory is described by **either**:

- ``SKILL.md`` — the preferred format: a YAML frontmatter block carrying the
  structured descriptor (anything :class:`~upgradelens.domain.skill.SkillPackage`
  accepts, including inline ``patterns`` / ``sources`` / ``patch_rules``),
  followed by a markdown body whose lead paragraph becomes ``description`` and
  whose ``## Limitations`` section becomes ``limitations``; or
- ``skill.yaml`` — the legacy descriptor mapping.

Optional sibling files (``patterns.yaml``, ``sources.yaml``,
``patch_rules.yaml``) are merged on top for both entry formats. The loader
validates version specifiers using :mod:`packaging`.
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

SKILL_MD_FILE = "SKILL.md"
SKILL_FILE = "skill.yaml"
PATTERNS_FILE = "patterns.yaml"
SOURCES_FILE = "sources.yaml"
PATCH_RULES_FILE = "patch_rules.yaml"

# Files that contribute to the content hash, in a fixed order so the hash is
# deterministic regardless of directory iteration order.
_HASHED_FILES = (SKILL_MD_FILE, SKILL_FILE, PATTERNS_FILE, SOURCES_FILE, PATCH_RULES_FILE)


class SkillParseError(Exception):
    """Raised when a Skill Pack directory cannot be parsed into a valid skill."""


def _read_yaml(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise SkillParseError(f"{path}: invalid YAML: {exc}") from exc


def _parse_skill_md(path: Path) -> dict[str, object]:
    """Parse a ``SKILL.md``: YAML frontmatter + markdown body.

    The frontmatter carries the structured descriptor. From the body, the lead
    paragraph (after the optional H1 title) becomes ``description`` and the
    ``## Limitations`` section becomes ``limitations``; explicit frontmatter
    keys win over the body-derived values.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillParseError(f"{path}: {SKILL_MD_FILE} must start with a '---' frontmatter fence")
    fence = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            fence = idx
            break
    if fence is None:
        raise SkillParseError(f"{path}: unterminated frontmatter (missing closing '---')")

    try:
        meta = yaml.safe_load("\n".join(lines[1:fence])) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"{path}: invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillParseError(f"{path}: frontmatter must be a mapping")

    desc_lines: list[str] = []
    lim_lines: list[str] = []
    in_limitations = False
    past_title = False
    for line in lines[fence + 1 :]:
        stripped = line.strip()
        if not past_title and stripped.startswith("# "):
            past_title = True
            continue
        if stripped.lower().startswith("## limitations"):
            in_limitations = True
            continue
        if in_limitations:
            lim_lines.append(line)
        else:
            desc_lines.append(line)

    meta.setdefault("description", "\n".join(desc_lines).strip())
    meta.setdefault("limitations", "\n".join(lim_lines).strip())
    return meta


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
    """Load and validate one Skill Pack from ``skill_dir``.

    ``SKILL.md`` takes precedence when present; otherwise the legacy
    ``skill.yaml`` descriptor is used. Sibling YAML files merge on top of
    inline frontmatter lists for both entry formats.
    """
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        raise SkillParseError(f"skill directory not found: {skill_dir}")

    md_path = skill_dir / SKILL_MD_FILE
    meta_path = skill_dir / SKILL_FILE
    if md_path.is_file():
        raw_meta: object = _parse_skill_md(md_path)
    elif meta_path.is_file():
        raw_meta = _read_yaml(meta_path)
    else:
        raise SkillParseError(f"missing {SKILL_MD_FILE} or {SKILL_FILE} in {skill_dir}")
    if not isinstance(raw_meta, dict):
        raise SkillParseError(f"{skill_dir}: skill descriptor must be a mapping")

    try:
        skill = SkillPackage.model_validate(raw_meta)
    except ValidationError as exc:
        raise SkillParseError(f"{skill_dir}: invalid skill descriptor: {exc}") from exc

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
        if child.is_dir() and ((child / SKILL_MD_FILE).is_file() or (child / SKILL_FILE).is_file()):
            skills.append(load_skill_package(child))
    return skills
