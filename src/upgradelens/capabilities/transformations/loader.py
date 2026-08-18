"""Load TransformationPacks from YAML (LS-2).

The canonical rewrite rules now live under ``capabilities/transformations/`` as
``<name>.yaml`` files, migrated out of the deprecated dependency-upgrade Skill
Packs. :func:`load_transformation_pack` reads one such file and builds a
:class:`~upgradelens.capabilities.transformation.TransformationPack`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from upgradelens.capabilities.transformation import TransformationPack
from upgradelens.domain.skill import PatchRule

_BUILTIN_DIR = Path(__file__).resolve().parent


class TransformationLoadError(ValueError):
    """Raised when a transformation YAML cannot be parsed."""


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            loaded: Any = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise TransformationLoadError(f"invalid transformation YAML {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TransformationLoadError(f"transformation YAML {path} must be a mapping")
    return loaded


def load_transformation_pack(name: str) -> TransformationPack:
    """Build a TransformationPack from ``capabilities/transformations/<name>.yaml``."""
    path = _BUILTIN_DIR / f"{name}.yaml"
    if not path.is_file():
        raise TransformationLoadError(f"no transformation pack named {name!r}")
    data = _load_yaml(path)
    rules = [PatchRule.model_validate(r) for r in data.get("rules", [])]
    return TransformationPack(
        id=str(data.get("id", name)),
        name=str(data.get("name", name)),
        allow_patch=bool(data.get("allow_patch", False)),
        rules=tuple(rules),
        package_names=tuple(str(p) for p in data.get("package_names", [])),
        target_version_spec=str(data.get("target_version_spec", "")),
    )


def discover_transformation_packs() -> list[TransformationPack]:
    if not _BUILTIN_DIR.is_dir():
        return []
    packs: list[TransformationPack] = []
    for path in sorted(_BUILTIN_DIR.glob("*.yaml")):
        packs.append(load_transformation_pack(path.stem))
    return packs


def load_transformation_packs(names: Iterable[str]) -> list[TransformationPack]:
    return [load_transformation_pack(n) for n in names]


def resolve_pack_for_dependency(dependency: str) -> TransformationPack | None:
    """Match a builtin TransformationPack by canonical package name (LS-1).

    Replaces the old ``TransformationPack.from_skill(outcome.skill)`` derivation:
    the mechanical-rewrite capability is resolved from the migrated YAML packs,
    so a dependency upgrade no longer needs the deprecated SkillPackage to be
    able to draft patches.
    """
    from packaging.utils import canonicalize_name

    canonical = canonicalize_name((dependency or "").strip())
    if not canonical:
        return None
    for pack in discover_transformation_packs():
        names = {canonicalize_name(str(n)) for n in pack.package_names}
        if canonical in names:
            return pack
    return None


__all__ = [
    "TransformationLoadError",
    "load_transformation_pack",
    "discover_transformation_packs",
    "load_transformation_packs",
    "resolve_pack_for_dependency",
]
