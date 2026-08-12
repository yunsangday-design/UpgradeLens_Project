"""Load and validate documentation source manifests (Step 4, S6).

A *source manifest* is the Skill-free way to add a dependency to the shared
corpus: a YAML file listing which documents describe an upgrade window, plus
the offline snapshots that back them.

.. code-block:: yaml

    schema_version: 1
    package_name: flask
    source_version_spec: ">=1.0,<2.0"
    target_version_spec: ">=2.0,<3.0"
    sources:
      - id: flask:2.0-changes
        url: https://flask.palletsprojects.com/en/2.0.x/changes/
        source_type: changelog
        snapshot: sources/flask-2.0-changes.md

Package and version fields declared at the top level are defaults: a source
may override any of them, and only fills them in when it differs.

Every failure is reported as :class:`DocSourceManifestError` with the file
path in the message, because these files are hand-authored and a silent skip
would look like "the corpus has no evidence" much later in the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from upgradelens.domain.doc_source_spec import DocSourceManifest, DocSourceSpec

#: Conventional file name discovered by :func:`discover_manifests`.
MANIFEST_FILENAME = "manifest.yaml"

#: Top-level keys that cascade into every source entry when the entry omits them.
_INHERITED_KEYS = ("package_name", "source_version_spec", "target_version_spec")


class DocSourceManifestError(ValueError):
    """A source manifest is missing, malformed, or internally inconsistent."""


def load_source_manifest(path: str | Path) -> DocSourceManifest:
    """Parse ``path`` into a validated :class:`DocSourceManifest`.

    Raises:
        DocSourceManifestError: the file is missing, is not valid YAML, declares
            no sources, or a source is missing its package/snapshot.
    """
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise DocSourceManifestError(f"source manifest not found: {manifest_path}")

    data = _load_mapping(manifest_path)
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise DocSourceManifestError(f"{manifest_path}: manifest declares no sources")

    specs: list[DocSourceSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_sources, start=1):
        spec = _build_spec(manifest_path, data, entry, index)
        if spec.id in seen:
            raise DocSourceManifestError(f"{manifest_path}: duplicate source id '{spec.id}'")
        seen.add(spec.id)
        specs.append(spec)

    payload = {
        "schema_version": data.get("schema_version", 1),
        "package_name": data.get("package_name", ""),
        "source_version_spec": data.get("source_version_spec", ""),
        "target_version_spec": data.get("target_version_spec", ""),
        "trust_level": data.get("trust_level"),
        "sources": specs,
        "base_dir": str(manifest_path.parent),
    }
    try:
        return DocSourceManifest.model_validate(payload)
    except ValidationError as exc:
        raise DocSourceManifestError(f"{manifest_path}: invalid manifest: {exc}") from exc


def discover_manifests(root: str | Path) -> list[Path]:
    """Return every ``manifest.yaml`` under ``root``, in a stable order.

    ``root`` may itself be a manifest file, so callers can accept "a manifest
    or a corpus directory" behind a single argument.
    """
    location = Path(root)
    if location.is_file():
        return [location]
    if not location.is_dir():
        raise DocSourceManifestError(f"corpus directory not found: {location}")
    return sorted(location.rglob(MANIFEST_FILENAME))


def resolve_snapshot(spec: DocSourceSpec, base_dir: str | Path) -> Path:
    """Resolve ``spec.snapshot`` against ``base_dir``.

    Snapshot paths must be relative and must stay inside the manifest's own
    directory: a manifest is data, and data must not be able to read arbitrary
    files just by writing ``../../..`` into a path.

    Raises:
        DocSourceManifestError: no snapshot declared, the path is absolute or
            escapes ``base_dir``, or the file does not exist.
    """
    if not spec.snapshot:
        raise DocSourceManifestError(f"source '{spec.id}' has no snapshot; cannot ingest offline")
    candidate = Path(spec.snapshot)
    if candidate.is_absolute():
        raise DocSourceManifestError(
            f"source '{spec.id}': snapshot path must be relative to the manifest directory"
        )
    base = Path(base_dir).resolve()
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(base):
        raise DocSourceManifestError(f"source '{spec.id}': snapshot escapes the manifest directory")
    if not resolved.is_file():
        raise DocSourceManifestError(f"documentation snapshot not found: {resolved}")
    return resolved


def _load_mapping(manifest_path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DocSourceManifestError(f"{manifest_path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise DocSourceManifestError(f"{manifest_path}: cannot be read: {exc}") from exc
    if data is None:
        raise DocSourceManifestError(f"{manifest_path}: manifest is empty")
    if not isinstance(data, dict):
        raise DocSourceManifestError(f"{manifest_path}: manifest must be a mapping")
    return data


def _build_spec(manifest_path: Path, data: dict[str, Any], entry: Any, index: int) -> DocSourceSpec:
    if not isinstance(entry, dict):
        raise DocSourceManifestError(f"{manifest_path}: source #{index} must be a mapping")

    merged = dict(entry)
    for key in _INHERITED_KEYS:
        inherited = data.get(key)
        if inherited and not merged.get(key):
            merged[key] = inherited
    trust_default = data.get("trust_level")
    if trust_default and not merged.get("trust_level"):
        merged["trust_level"] = trust_default

    try:
        spec = DocSourceSpec.model_validate(merged)
    except ValidationError as exc:
        raise DocSourceManifestError(f"{manifest_path}: source #{index} is invalid: {exc}") from exc

    if not spec.package_name:
        raise DocSourceManifestError(
            f"{manifest_path}: source '{spec.id}' has no package_name; "
            "a corpus document must say which dependency it describes"
        )
    if not spec.snapshot:
        raise DocSourceManifestError(
            f"{manifest_path}: source '{spec.id}' has no snapshot; cannot ingest offline"
        )
    return spec


__all__ = [
    "MANIFEST_FILENAME",
    "DocSourceManifestError",
    "discover_manifests",
    "load_source_manifest",
    "resolve_snapshot",
]
