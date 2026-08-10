"""Skill-independent description of a documentation source (Step 4, S6).

Until S5 the only way to tell UpgradeLens where a dependency's documentation
lived was to author a Skill Pack. That bound a *fact* ("this URL documents
pydantic 2.x") to a *capability* ("how to rewrite ``@validator``") -- exactly
the coupling the shared-corpus design removes: adding a dependency to the
corpus must never require adding a Skill.

A :class:`DocSourceSpec` therefore carries only what the shared corpus needs
to index and later cite a document: which package and version window it
covers, where it came from, how far it can be trusted, and which offline
snapshot backs it. A :class:`DocSourceManifest` is the YAML-authored list of
such specs for one package.

These three ``Literal`` aliases live here rather than in
:mod:`upgradelens.domain.skill` so that the dependency points *away* from the
Skill model: doc sources are a corpus concept that Skills happen to reuse,
not the other way round.
"""

from __future__ import annotations

from typing import Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["official_doc", "changelog", "migration_guide"]
TrustLevel = Literal["official", "community", "unverified"]
FetchStrategy = Literal["html", "markdown", "static"]


class DocSourceSpec(BaseModel):
    """One document in the shared corpus, described independently of any Skill.

    ``package_name`` and the two version specs are what make a document
    *findable by dependency alone*: retrieval starts from "which package, from
    which version, to which version", never from a Skill id.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable identifier, also the FTS/vector partition key for this document.
    id: str = Field(..., min_length=1)
    #: Dependency this document is about. Empty means "not part of the shared
    #: corpus" -- tolerated for legacy rows, rejected by the manifest loader.
    package_name: str = ""
    url: str = ""
    #: Human-readable label; falls back to :attr:`id` via :attr:`display_title`.
    title: str = ""
    source_type: SourceType = "official_doc"
    trust_level: TrustLevel = "official"
    #: PEP 440 specifier for the versions this document describes upgrading FROM.
    source_version_spec: str = ""
    #: PEP 440 specifier for the versions this document describes upgrading TO.
    target_version_spec: str = ""
    fetch_strategy: FetchStrategy = "static"
    parse_strategy: str | None = None
    #: Path to the offline snapshot, relative to the manifest directory.
    snapshot: str = ""

    @property
    def canonical_package(self) -> str:
        """PEP 503-normalised package name, or ``""`` when unset."""
        return canonicalize_name(self.package_name) if self.package_name else ""

    @property
    def display_title(self) -> str:
        return self.title or self.id


class DocSourceManifest(BaseModel):
    """A YAML-authored set of doc sources, normally one file per package.

    The package/version fields act as defaults for every source in the file so
    a manifest stays readable: state the upgrade window once, list the URLs
    below it.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    package_name: str = ""
    source_version_spec: str = ""
    target_version_spec: str = ""
    trust_level: TrustLevel | None = None
    sources: tuple[DocSourceSpec, ...] = ()
    #: Directory the snapshots are resolved against. Filled in by the loader,
    #: never authored in YAML.
    base_dir: str = ""

    @property
    def canonical_package(self) -> str:
        return canonicalize_name(self.package_name) if self.package_name else ""


__all__ = [
    "DocSourceManifest",
    "DocSourceSpec",
    "FetchStrategy",
    "SourceType",
    "TrustLevel",
]
