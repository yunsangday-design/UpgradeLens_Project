"""S6: Skill-free documentation ingestion via source manifests.

The point of these tests is a single property: **a dependency can enter the
shared corpus without a Skill Pack existing for it.** Everything else here
guards the edges that would silently produce an empty corpus (a typo'd
snapshot path, a source with no package tag) or a corrupted one (re-ingesting
duplicating chunks).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs.ingest import (
    ingest_corpus,
    ingest_manifest_file,
    ingest_skill,
    ingest_source_spec,
    iter_sources_for_package,
)
from upgradelens.docs.retrieval import retrieve
from upgradelens.docs.source_manifest import (
    DocSourceManifestError,
    discover_manifests,
    load_source_manifest,
    resolve_snapshot,
)
from upgradelens.domain.doc_source_spec import DocSourceSpec
from upgradelens.skills import builtin_registry
from upgradelens.skills.compat import skill_to_source_specs

SNAPSHOT = """# Flask 2.0 changes

## Deprecated flask.json

`flask.json.JSONEncoder` is deprecated and will be removed. Configure
`app.json_provider_class` instead.

## Removed flask.Markup

`flask.Markup` was removed; import `markupsafe.Markup` directly.
"""


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    engine = engine_for(tmp_path / "corpus.db")
    init_db(engine)
    return session_for(engine)()


def _write_corpus(root: Path, *, manifest_body: str, snapshot: str = SNAPSHOT) -> Path:
    package_dir = root / "flask"
    (package_dir / "sources").mkdir(parents=True, exist_ok=True)
    (package_dir / "sources" / "flask-2.0-changes.md").write_text(snapshot, encoding="utf-8")
    manifest_path = package_dir / "manifest.yaml"
    manifest_path.write_text(manifest_body, encoding="utf-8")
    return manifest_path


_MANIFEST = """
schema_version: 1
package_name: Flask
source_version_spec: ">=1.0,<2.0"
target_version_spec: ">=2.0,<3.0"
sources:
  - id: flask:2.0-changes
    url: https://flask.palletsprojects.com/en/2.0.x/changes/
    title: Flask 2.0 changes
    source_type: changelog
    snapshot: sources/flask-2.0-changes.md
"""


# --------------------------------------------------------------------------- #
# Manifest parsing
# --------------------------------------------------------------------------- #


def test_manifest_cascades_package_and_versions_into_sources(tmp_path: Path) -> None:
    """Top-level package/version fields are defaults, so entries stay readable."""
    manifest_path = _write_corpus(tmp_path, manifest_body=_MANIFEST)

    manifest = load_source_manifest(manifest_path)

    assert len(manifest.sources) == 1
    spec = manifest.sources[0]
    assert spec.package_name == "Flask"
    assert spec.canonical_package == "flask"
    assert spec.source_version_spec == ">=1.0,<2.0"
    assert spec.target_version_spec == ">=2.0,<3.0"
    assert manifest.base_dir == str(manifest_path.parent)


def test_source_entry_overrides_manifest_defaults(tmp_path: Path) -> None:
    body = _MANIFEST + """  - id: flask:3.0-changes
    url: https://flask.palletsprojects.com/en/3.0.x/changes/
    target_version_spec: ">=3.0,<4.0"
    snapshot: sources/flask-2.0-changes.md
"""
    manifest_path = _write_corpus(tmp_path, manifest_body=body)

    manifest = load_source_manifest(manifest_path)

    assert [s.target_version_spec for s in manifest.sources] == [">=2.0,<3.0", ">=3.0,<4.0"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("schema_version: 1\nsources: []\n", "declares no sources"),
        (
            "sources:\n  - id: x\n    snapshot: sources/flask-2.0-changes.md\n",
            "has no package_name",
        ),
        ("package_name: flask\nsources:\n  - id: x\n", "has no snapshot"),
        (
            "package_name: flask\nsources:\n"
            "  - id: dup\n    snapshot: sources/flask-2.0-changes.md\n"
            "  - id: dup\n    snapshot: sources/flask-2.0-changes.md\n",
            "duplicate source id",
        ),
    ],
)
def test_invalid_manifest_is_rejected_with_a_pointed_message(
    tmp_path: Path, body: str, expected: str
) -> None:
    """Hand-authored data fails loudly: a silent skip looks like 'no evidence'."""
    manifest_path = _write_corpus(tmp_path, manifest_body=body)

    with pytest.raises(DocSourceManifestError, match=expected):
        load_source_manifest(manifest_path)


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DocSourceManifestError, match="source manifest not found"):
        load_source_manifest(tmp_path / "nope.yaml")


def test_snapshot_cannot_escape_the_manifest_directory(tmp_path: Path) -> None:
    """A manifest is data; data must not be able to read arbitrary files."""
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    spec = DocSourceSpec(id="x", package_name="flask", snapshot="../secret.md")

    with pytest.raises(DocSourceManifestError, match="escapes the manifest directory"):
        resolve_snapshot(spec, tmp_path / "flask")


def test_missing_snapshot_file_is_reported(tmp_path: Path) -> None:
    spec = DocSourceSpec(id="x", package_name="flask", snapshot="sources/absent.md")

    with pytest.raises(DocSourceManifestError, match="snapshot not found"):
        resolve_snapshot(spec, tmp_path)


def test_discover_manifests_walks_a_corpus_tree(tmp_path: Path) -> None:
    manifest_path = _write_corpus(tmp_path, manifest_body=_MANIFEST)

    assert discover_manifests(tmp_path) == [manifest_path]
    assert discover_manifests(manifest_path) == [manifest_path]


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


def test_manifest_ingestion_makes_a_package_retrievable_without_any_skill(
    tmp_path: Path, session: Session
) -> None:
    """The S6 acceptance property: no Skill Pack for flask exists, yet it works."""
    assert builtin_registry().get("flask") is None
    manifest_path = _write_corpus(tmp_path, manifest_body=_MANIFEST)

    records = ingest_manifest_file(session, manifest_path)

    assert [r.id for r in records] == ["flask:2.0-changes"]
    assert records[0].package_name == "flask"
    assert records[0].chunk_count > 0
    assert [r.id for r in iter_sources_for_package(session, "flask")] == ["flask:2.0-changes"]

    run = retrieve(session, "flask:2.0-changes", "JSONEncoder", top_k=3)
    assert run.matched_chunk_ids, "ingested manifest content must be retrievable"


def test_ingested_source_row_carries_corpus_metadata(tmp_path: Path, session: Session) -> None:
    manifest_path = _write_corpus(tmp_path, manifest_body=_MANIFEST)

    ingest_manifest_file(session, manifest_path)

    row = session.get(models.DocSourceRow, "flask:2.0-changes")
    assert row is not None
    assert row.package_name == "flask"
    assert row.source_version_spec == ">=1.0,<2.0"
    assert row.target_version_spec == ">=2.0,<3.0"
    assert row.title == "Flask 2.0 changes"
    assert row.trust_level == "official"
    assert row.snapshot_hash


def test_reingesting_replaces_chunks_instead_of_duplicating(
    tmp_path: Path, session: Session
) -> None:
    manifest_path = _write_corpus(tmp_path, manifest_body=_MANIFEST)
    first = ingest_manifest_file(session, manifest_path)

    second = ingest_manifest_file(session, manifest_path)

    stored = (
        session.execute(
            select(models.DocChunkRow).where(models.DocChunkRow.source_id == "flask:2.0-changes")
        )
        .scalars()
        .all()
    )
    assert len(stored) == first[0].chunk_count == second[0].chunk_count


def test_ingest_corpus_walks_every_package_in_the_tree(tmp_path: Path, session: Session) -> None:
    _write_corpus(tmp_path, manifest_body=_MANIFEST)
    other = tmp_path / "httpx"
    (other / "sources").mkdir(parents=True)
    (other / "sources" / "notes.md").write_text("# httpx\n\nSome note.\n", encoding="utf-8")
    (other / "manifest.yaml").write_text(
        "package_name: httpx\nsources:\n  - id: httpx:notes\n    snapshot: sources/notes.md\n",
        encoding="utf-8",
    )

    records = ingest_corpus(session, tmp_path)

    assert sorted(r.package_name for r in records) == ["flask", "httpx"]


def test_ingest_source_spec_rejects_a_missing_snapshot(tmp_path: Path, session: Session) -> None:
    spec = DocSourceSpec(id="x", package_name="flask", snapshot="sources/absent.md")

    with pytest.raises(DocSourceManifestError):
        ingest_source_spec(session, spec, base_dir=tmp_path)


# --------------------------------------------------------------------------- #
# Skill compatibility layer
# --------------------------------------------------------------------------- #


def test_skill_sources_translate_to_specs_preserving_corpus_scoping() -> None:
    skill = builtin_registry().get("pydantic_v1_to_v2")
    assert skill is not None

    specs = skill_to_source_specs(skill)

    assert specs, "the built-in skill must still expose ingestable sources"
    assert {s.canonical_package for s in specs} == {"pydantic"}
    assert all(s.source_version_spec == skill.source_version_spec for s in specs)
    assert all(s.snapshot for s in specs)


def test_skill_ingestion_still_works_through_the_generic_path(session: Session) -> None:
    """Deprecated, but the built-in Skills must keep ingesting until migrated."""
    skill = builtin_registry().get("pydantic_v1_to_v2")
    assert skill is not None

    records = ingest_skill(session, skill)

    assert records
    assert all(r.package_name == "pydantic" for r in records)
    assert iter_sources_for_package(session, "pydantic")
