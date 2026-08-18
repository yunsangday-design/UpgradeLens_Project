"""Domain models for UpgradeLens Skill Packs (plan sections 3, 8.9-8.12, 9.1-9.8).

A *Skill Pack* is a directory of YAML files that teaches UpgradeLens how to
analyse a particular dependency upgrade. Skills are loaded from disk by the
:mod:`upgradelens.skills` package, so adding a new dependency's know-how never
requires touching the core workflow (plan section 3, line 1709: "新增 Skill 不需要
修改主工作流").
"""

from __future__ import annotations

from typing import Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

from upgradelens.domain.doc_source_spec import FetchStrategy, SourceType, TrustLevel

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

SupportStatus = Literal["dedicated", "generic", "experimental"]
PatternKind = Literal[
    "import",
    "decorator",
    "method_call",
    "attribute_access",
    "class_config",
    "class_base",
]
Severity = Literal["info", "low", "medium", "high"]
Confidence = Literal["high", "low", "uncertain"]
PatchRiskLevel = Literal["low", "medium", "high"]

# ``SourceType`` / ``TrustLevel`` / ``FetchStrategy`` now live with the
# Skill-independent corpus model (S6) and are re-exported here so existing
# importers keep working while the Skill doc-source path is retired.

# Standard lowered-capability statement emitted whenever the generic fallback is
# selected (plan section 3, line 1711: "Generic 模式明确降低能力声明").
GENERIC_CAPABILITY_NOTE = (
    "Generic fallback: no dedicated knowledge pack is available for this "
    "dependency, so capability claims are reduced. Findings are advisory and "
    "require manual confirmation before any migration decision."
)

# Appended to a selection's capability note when the chosen pack is deprecated
# (MA / LS migration): the shared RAG corpus + TransformationPack + AgentSkill
# path supersedes it.
DEPRECATED_SELECTION_NOTE = (
    " [DEPRECATED] This Skill Pack is superseded by the shared RAG corpus + "
    "TransformationPack + AgentSkill path; prefer the generic fallback or set "
    "UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION to opt out."
)


# ---------------------------------------------------------------------------
# Patterns — what usage looks like, and how to retrieve docs for it
# ---------------------------------------------------------------------------


class UsagePattern(BaseModel):
    """A single syntactic/semantic pattern a skill knows how to recognise."""

    id: str
    kind: PatternKind
    match: str = Field(
        ...,
        description="Symbol/attribute name or '*' wildcard that triggers this pattern.",
    )
    usage_type: str | None = Field(
        default=None, description="Free-text category shown to the user."
    )
    risk_hint: str = Field(default="", description="Human-readable risk description.")
    retrieval_queries: list[str] = Field(default_factory=list)
    severity: Severity = "medium"
    confidence: Confidence = "high"


# ---------------------------------------------------------------------------
# Sources — where the trustworthy documentation lives
# ---------------------------------------------------------------------------


class DocSource(BaseModel):
    """A Skill-declared documentation source.

    Deprecated since S6: doc sources are corpus facts, not skill capabilities.
    New corpora declare
    :class:`~upgradelens.domain.doc_source_spec.DocSourceSpec` entries in a
    source manifest instead; this model survives only for the built-in Skills
    and the live-fetch path, via :mod:`upgradelens.skills.compat`.
    """

    id: str
    url: str
    source_type: SourceType = "official_doc"
    trust_level: TrustLevel = "official"
    target_version_spec: str | None = Field(
        default=None,
        description="PEP 440 specifier for the target versions this doc covers.",
    )
    fixture_snapshot: str | None = Field(
        default=None,
        description="Relative path to a captured doc snapshot for offline use.",
    )
    fetch_strategy: FetchStrategy = "static"
    parse_strategy: str | None = None


# ---------------------------------------------------------------------------
# Patch whitelist — which mechanical rewrites may be proposed later
# ---------------------------------------------------------------------------


class PatchRule(BaseModel):
    id: str
    precondition: str = Field(default="", description="When this rewrite may be applied.")
    forbid_condition: str = Field(default="", description="Situation that blocks the rewrite.")
    target_pattern: str = Field(..., description="The code shape the rewrite targets.")
    replacement_template: str = Field(..., description="The proposed replacement shape.")
    # Optional regex form: a capture-group-aware alternative to the literal
    # pattern above. When present, the generator uses ``re.sub`` so field names
    # and similar captured text survive the rewrite (e.g. @validator('name') ->
    # @field_validator('name')).
    target_regex: str | None = Field(
        default=None, description="Optional regex matching the code shape to rewrite."
    )
    replacement: str | None = Field(
        default=None, description="Replacement template for target_regex, may use \\1 etc."
    )
    required_evidence: list[str] = Field(default_factory=list)
    requires_quality_model: bool = False
    patch_risk_level: PatchRiskLevel = "low"


# ---------------------------------------------------------------------------
# Skill package — the top-level descriptor (skill.yaml + sibling YAML files)
# ---------------------------------------------------------------------------


class SkillPackage(BaseModel):
    """The fully-resolved description of one Skill Pack."""

    skill_id: str
    name: str = Field(default="", description="Human-readable skill name.")
    package_names: list[str] = Field(
        default_factory=list,
        description="Canonical dependency names plus aliases this skill applies to.",
    )
    source_version_spec: str | None = Field(
        default=None,
        description="PEP 440 specifier for source versions this skill migrates FROM.",
    )
    target_version_spec: str | None = Field(
        default=None,
        description="PEP 440 specifier for target versions this skill migrates TO.",
    )
    priority: int = Field(default=0, ge=0)
    support_status: SupportStatus = "dedicated"
    risk_categories: list[str] = Field(default_factory=list)
    allow_patch_draft: bool = False
    description: str = ""
    limitations: str = ""
    version: str = "1.0.0"
    patterns: list[UsagePattern] = Field(default_factory=list)
    sources: list[DocSource] = Field(default_factory=list)
    patch_rules: list[PatchRule] = Field(default_factory=list)
    # Migration flag (MA / LS): marks a legacy dependency-upgrade Skill Pack as
    # superseded by the shared RAG corpus + TransformationPack + AgentSkill path.
    # Deprecated packs still load and run, but ``select_skill`` flags them and
    # (when UPGRADELENS_LEGACY_SKILL_DISABLE_SELECTION is set) falls back to
    # generic instead of selecting them.
    deprecated: bool = False
    # Metadata filled in by the loader, not authored in YAML.
    content_hash: str = Field(default="", repr=False)
    source_path: str = Field(default="", repr=False)

    @property
    def is_generic(self) -> bool:
        return self.support_status == "generic"

    @property
    def canonical_package_names(self) -> set[str]:
        return {canonicalize_name(name) for name in self.package_names}


# ---------------------------------------------------------------------------
# Loader outputs used by the rest of the pipeline
# ---------------------------------------------------------------------------


class SkillSelection(BaseModel):
    """The result of choosing a Skill Pack for a concrete upgrade request."""

    skill_id: str
    package_name: str
    skill_version: str
    support_status: SupportStatus
    is_generic: bool
    priority: int
    matched_by: Literal["version_range", "generic_fallback"]
    content_hash: str
    capability_note: str = ""
    deprecated: bool = False


class SkillCatalogEntry(BaseModel):
    skill_id: str
    name: str
    package_names: list[str]
    source_version_spec: str | None
    target_version_spec: str | None
    priority: int
    support_status: SupportStatus
    pattern_count: int
    source_count: int
    patch_rule_count: int
    skill_version: str
    content_hash: str
    deprecated: bool = False


class SkillCatalog(BaseModel):
    skills: list[SkillCatalogEntry]
    generic_skill_id: str | None = None


def catalog_entry_from_skill(skill: SkillPackage) -> SkillCatalogEntry:
    return SkillCatalogEntry(
        skill_id=skill.skill_id,
        name=skill.name,
        package_names=skill.package_names,
        source_version_spec=skill.source_version_spec,
        target_version_spec=skill.target_version_spec,
        priority=skill.priority,
        support_status=skill.support_status,
        pattern_count=len(skill.patterns),
        source_count=len(skill.sources),
        patch_rule_count=len(skill.patch_rules),
        skill_version=skill.version,
        content_hash=skill.content_hash,
        deprecated=skill.deprecated,
    )


def selection_from_skill(
    skill: SkillPackage,
    package_name: str,
    *,
    matched_by: Literal["version_range", "generic_fallback"],
) -> SkillSelection:
    return SkillSelection(
        skill_id=skill.skill_id,
        package_name=package_name,
        skill_version=skill.version,
        support_status=skill.support_status,
        is_generic=skill.is_generic,
        priority=skill.priority,
        matched_by=matched_by,
        content_hash=skill.content_hash,
        capability_note=(
            (GENERIC_CAPABILITY_NOTE if skill.is_generic else "")
            + (DEPRECATED_SELECTION_NOTE if skill.deprecated else "")
        ),
        deprecated=skill.deprecated,
    )
