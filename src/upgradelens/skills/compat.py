"""Deprecated bridge from Skill-owned doc sources to the shared corpus (Step 4, S6).

Skill Packs used to own their documentation sources, which made "teach the
system about a dependency" and "teach the system how to rewrite that
dependency's code" the same action. S6 splits them: facts enter the corpus
through a :class:`~upgradelens.domain.doc_source_spec.DocSourceManifest`, and
a Skill is only ever an optional capability on top.

The built-in Skills still carry ``sources:`` blocks, so this module translates
them into specs and lets the generic ingestion path do the work. It is a
migration shim with no logic of its own -- new corpora must ship a manifest,
and once the built-in Skills are converted this module can be deleted.
"""

from __future__ import annotations

from packaging.utils import canonicalize_name

from upgradelens.domain.doc_source_spec import DocSourceSpec
from upgradelens.domain.skill import DocSource, SkillPackage


def skill_package_name(skill: SkillPackage) -> str:
    """The canonical dependency a skill's documents belong to.

    A skill may list aliases; the first entry is the canonical one. The generic
    fallback skill declares none, in which case its documents are not tagged
    and stay outside the shared corpus -- which is correct, they describe no
    particular dependency.
    """
    return canonicalize_name(skill.package_names[0]) if skill.package_names else ""


def skill_source_to_spec(skill: SkillPackage, source: DocSource) -> DocSourceSpec:
    """Translate one Skill-declared doc source into a corpus spec.

    Version scoping follows the pre-S6 behaviour exactly: the from-version
    comes from the skill (a source never declared one) and the to-version from
    the source, falling back to the skill's.
    """
    return DocSourceSpec(
        id=source.id,
        package_name=skill_package_name(skill),
        url=source.url,
        title=source.id,
        source_type=source.source_type,
        trust_level=source.trust_level,
        source_version_spec=skill.source_version_spec or "",
        target_version_spec=source.target_version_spec or skill.target_version_spec or "",
        fetch_strategy=source.fetch_strategy,
        parse_strategy=source.parse_strategy,
        snapshot=source.fixture_snapshot or "",
    )


def skill_to_source_specs(skill: SkillPackage) -> list[DocSourceSpec]:
    """Every offline-ingestable source of ``skill``, as corpus specs.

    Sources without a snapshot are skipped: they can only be fetched live, and
    that path has its own entry point.
    """
    return [
        skill_source_to_spec(skill, source) for source in skill.sources if source.fixture_snapshot
    ]


__all__ = ["skill_package_name", "skill_source_to_spec", "skill_to_source_specs"]
