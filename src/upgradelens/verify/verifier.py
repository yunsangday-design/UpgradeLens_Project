"""Evidence Verifier (plan section 13.1/13.2).

Takes the raw :class:`~upgradelens.models.impact.ImpactReport` produced by the
LangGraph loop and re-checks every claim against ground truth on disk and in
the evidence bundle. A risk only becomes ``verified`` if it survives all
checks; anything else is surfaced separately as a degraded finding.

The verifier never calls a model and never mutates the repository.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.domain.skill import SkillPackage
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport
from upgradelens.platform import read_text_utf8
from upgradelens.verify.models import (
    BLOCKING_ISSUES,
    Conclusion,
    EvidenceStatus,
    IssueCode,
    TestCandidate,
    VerificationIssue,
    VerifiedReport,
    VerifiedRisk,
)
from upgradelens.verify.recommender import rank_tests, recommend_tests
from upgradelens.verify.risk_rules import RiskScoringInput, score_risk
from upgradelens.verify.version_match import extract_version

__all__ = ["EvidenceVerifier", "verify_report"]

_CODE_KINDS = frozenset({"code_usage", "parse_error", "dynamic_import"})

#: Symbols shorter than this are too generic to be matched against free text
#: without producing false "the title mentions X" hits.
_MIN_GROUNDED_SYMBOL_LEN = 3


def _mentions_symbol(text: str, symbol: str) -> bool:
    """True when ``symbol`` appears in ``text`` as a whole identifier.

    Word boundaries matter: ``validator`` must not be considered "mentioned" by
    a title that only talks about ``field_validator``.
    """
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


@dataclass(frozen=True)
class _FileState:
    """Ground truth for one repository file at verification time."""

    exists: bool
    line_count: int
    content_hash: str


class EvidenceVerifier:
    """Re-checks a model report against real code, docs and version metadata."""

    def __init__(
        self,
        *,
        repo_root: Path,
        bundle: EvidenceBundle,
        code_report: CodeEvidenceReport,
        skill: SkillPackage | None = None,
    ) -> None:
        self._root = Path(repo_root)
        self._bundle = bundle
        self._code_report = code_report
        self._skill = skill
        self._file_cache: dict[str, _FileState] = {}

    # -- file ground truth -------------------------------------------------

    def _file_state(self, rel_path: str) -> _FileState:
        cached = self._file_cache.get(rel_path)
        if cached is not None:
            return cached
        path = self._root / rel_path
        if not path.is_file():
            state = _FileState(exists=False, line_count=0, content_hash="")
        else:
            try:
                text = read_text_utf8(path)
            except (OSError, UnicodeDecodeError):
                state = _FileState(exists=False, line_count=0, content_hash="")
            else:
                state = _FileState(
                    exists=True,
                    line_count=len(text.splitlines()),
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
        self._file_cache[rel_path] = state
        return state

    def _check_code_item(self, item: EvidenceItem) -> list[VerificationIssue]:
        """Verify that a code evidence item still points at real source."""
        issues: list[VerificationIssue] = []
        rel_path = str(item.meta.get("path", ""))
        if not rel_path:
            return issues
        state = self._file_state(rel_path)
        if not state.exists:
            issues.append(
                VerificationIssue(
                    code=IssueCode.FILE_NOT_FOUND,
                    detail=f"{rel_path} does not exist in the repository",
                    evidence_id=item.evidence_id,
                )
            )
            return issues

        line = item.meta.get("line")
        if isinstance(line, int) and (line < 1 or line > state.line_count):
            issues.append(
                VerificationIssue(
                    code=IssueCode.LINE_OUT_OF_RANGE,
                    detail=f"{rel_path}:{line} is outside the file (1..{state.line_count})",
                    evidence_id=item.evidence_id,
                )
            )

        recorded = str(item.meta.get("content_hash", ""))
        if recorded and recorded != state.content_hash:
            issues.append(
                VerificationIssue(
                    code=IssueCode.CONTENT_HASH_CHANGED,
                    detail=f"{rel_path} changed since the scan; evidence is stale",
                    evidence_id=item.evidence_id,
                )
            )
        return issues

    # -- documentation checks ---------------------------------------------

    def _doc_source_spec(self, item: EvidenceItem) -> tuple[str | None, str]:
        """Return ``(target_version_spec, trust_level)`` for a doc evidence item.

        The evidence itself is the source of truth: the shared corpus records the
        version window and trust level of every chunk, so a document can be
        version-checked whether or not a Skill Pack exists. The Skill lookup is
        only a fallback for legacy evidence that predates those meta fields.
        """
        spec_text = str(item.meta.get("target_version_spec", "") or "") or None
        trust = str(item.meta.get("trust_level", "") or "")
        if spec_text is not None or trust:
            return spec_text, trust or "unverified"

        source_id = str(item.meta.get("source_id", ""))
        if self._skill is not None:
            for source in self._skill.sources:
                if source.id == source_id:
                    return source.target_version_spec, source.trust_level
        return None, "unverified"

    def _check_doc_item(
        self, item: EvidenceItem, target_version_spec: str
    ) -> list[VerificationIssue]:
        """Flag documentation that clearly does not cover the target version."""
        issues: list[VerificationIssue] = []
        source_id = str(item.meta.get("source_id", ""))
        spec_text, trust = self._doc_source_spec(item)

        if trust not in ("official",):
            issues.append(
                VerificationIssue(
                    code=IssueCode.DOC_SOURCE_UNTRUSTED,
                    detail=f"doc source '{source_id}' has trust level '{trust}'",
                    evidence_id=item.evidence_id,
                )
            )

        target_version = extract_version(target_version_spec)
        if not spec_text or target_version is None:
            return issues
        try:
            specifier = SpecifierSet(spec_text)
            covered = specifier.contains(Version(target_version), prereleases=True)
        except (InvalidSpecifier, InvalidVersion):
            return issues
        if not covered:
            issues.append(
                VerificationIssue(
                    code=IssueCode.DOC_VERSION_CONFLICT,
                    detail=(
                        f"doc source '{source_id}' covers '{spec_text}' "
                        f"which excludes target {target_version}"
                    ),
                    evidence_id=item.evidence_id,
                )
            )
        return issues

    # -- symbol grounding --------------------------------------------------

    def _repo_symbols(self) -> set[str]:
        """Every dependency API symbol the scan observed anywhere in the repository.

        This vocabulary replaces the curated skill pattern list: it is derived
        from the code evidence, so the check works for any dependency.
        """
        symbols = {str(i.meta.get("symbol", "")) for i in self._bundle.by_kind("code_usage")}
        symbols.discard("")
        return {s for s in symbols if len(s) >= _MIN_GROUNDED_SYMBOL_LEN}

    def _check_symbol_grounding(
        self, title: str, code_items: list[EvidenceItem]
    ) -> list[VerificationIssue]:
        """Ensure a named API in the title actually appears in the cited evidence.

        The title is matched against the symbols the scan really found. When the
        title names one of them but the risk cites code evidence for a different
        symbol, the risk is pointing at the wrong location.
        """
        if not code_items:
            return []
        cited = {str(i.meta.get("symbol", "")) for i in code_items}
        cited.discard("")
        for symbol in sorted(self._repo_symbols()):
            if symbol in cited or not _mentions_symbol(title, symbol):
                continue
            return [
                VerificationIssue(
                    code=IssueCode.SYMBOL_NOT_IN_EVIDENCE,
                    detail=(
                        f"title mentions '{symbol}' but no cited code evidence uses it "
                        f"(cited symbols: {sorted(cited) or 'none'})"
                    ),
                )
            ]
        return []

    # -- status decision ---------------------------------------------------

    @staticmethod
    def _decide_status(
        *,
        issues: list[VerificationIssue],
        has_code: bool,
        has_doc: bool,
    ) -> EvidenceStatus:
        """Map the collected issues onto one evidence status.

        Order matters: conflicting evidence is reported as such even when other
        soft issues exist, and any blocking issue always wins over "verified".
        """
        codes = {issue.code for issue in issues}
        if IssueCode.DOC_VERSION_CONFLICT in codes:
            return EvidenceStatus.CONFLICTING_EVIDENCE
        if codes & BLOCKING_ISSUES:
            return EvidenceStatus.INSUFFICIENT_EVIDENCE
        if not has_code:
            return EvidenceStatus.INSUFFICIENT_EVIDENCE
        if not has_doc or IssueCode.SYMBOL_NOT_IN_EVIDENCE in codes:
            return EvidenceStatus.PARTIALLY_VERIFIED
        if IssueCode.DOC_SOURCE_UNTRUSTED in codes:
            return EvidenceStatus.PARTIALLY_VERIFIED
        return EvidenceStatus.VERIFIED

    # -- public API --------------------------------------------------------

    def verify(
        self,
        report: ImpactReport,
        *,
        degradations: list[str] | None = None,
    ) -> VerifiedReport:
        """Verify every risk in ``report`` and build the auditable output."""
        degradations = list(degradations or [])
        verified: list[VerifiedRisk] = []
        degraded: list[VerifiedRisk] = []
        per_risk_tests: dict[str, list[TestCandidate]] = {}
        severities: dict[str, str] = {}

        for risk in report.risks:
            checked = self._verify_one(risk, report)
            per_risk_tests[checked.risk_id] = checked.recommended_tests
            severities[checked.risk_id] = checked.severity
            if checked.is_verified:
                verified.append(checked)
            else:
                degraded.append(checked)

        conclusion = self._conclude(verified, degraded)

        has_parse_error = bool(self._bundle.by_kind("parse_error"))
        if has_parse_error:
            degradations.append("Some files could not be parsed; coverage is incomplete.")

        return VerifiedReport(
            target_dependency=report.target_dependency,
            source_version_spec=report.source_version_spec,
            source_version_source=report.source_version_source,
            target_version_spec=report.target_version_spec,
            conclusion=conclusion,
            verified_risks=verified,
            degraded_risks=degraded,
            recommended_tests=rank_tests(per_risk_tests, severities),
            evidence_summary=dict(report.evidence_summary),
            partial=bool(degradations),
            degradations=degradations,
            static=report.static,
            notes=report.notes,
        )

    def _verify_one(self, risk: object, report: ImpactReport) -> VerifiedRisk:
        # ``risk`` is a RiskItem; typed loosely to keep the import surface small.
        risk_id = getattr(risk, "risk_id", "")
        title = getattr(risk, "title", "")
        model_severity = str(getattr(risk, "severity", "low"))
        recommendation = str(getattr(risk, "recommendation", ""))
        evidence_ids: list[str] = list(getattr(risk, "evidence_ids", []))

        issues: list[VerificationIssue] = []
        code_items: list[EvidenceItem] = []
        doc_items: list[EvidenceItem] = []
        unknown: list[str] = []

        if not evidence_ids:
            issues.append(
                VerificationIssue(
                    code=IssueCode.NO_EVIDENCE_IDS,
                    detail="risk cites no evidence at all",
                )
            )

        for evidence_id in evidence_ids:
            item = self._bundle.get(evidence_id)
            if item is None:
                unknown.append(evidence_id)
                issues.append(
                    VerificationIssue(
                        code=IssueCode.UNKNOWN_EVIDENCE_ID,
                        detail=f"evidence id '{evidence_id}' does not exist in the bundle",
                        evidence_id=evidence_id,
                    )
                )
                continue
            if item.kind in _CODE_KINDS:
                code_items.append(item)
            elif item.kind == "doc_chunk":
                doc_items.append(item)

        for item in code_items:
            if item.kind == "code_usage":
                issues.extend(self._check_code_item(item))
            elif item.kind == "dynamic_import":
                issues.extend(self._check_code_item(item))

        real_usages = [i for i in code_items if i.kind == "code_usage"]
        if evidence_ids and not code_items:
            issues.append(
                VerificationIssue(
                    code=IssueCode.NO_CODE_EVIDENCE,
                    detail="risk cites documentation only; no code location backs it",
                )
            )
        elif code_items and not real_usages:
            issues.append(
                VerificationIssue(
                    code=IssueCode.DYNAMIC_ONLY_EVIDENCE,
                    detail="only dynamic imports or parse errors back this risk",
                )
            )

        if not doc_items:
            issues.append(
                VerificationIssue(
                    code=IssueCode.NO_DOC_EVIDENCE,
                    detail="no official documentation chunk supports this risk",
                )
            )
        for item in doc_items:
            issues.extend(self._check_doc_item(item, report.target_version_spec))

        issues.extend(self._check_symbol_grounding(title, real_usages))

        status = self._decide_status(
            issues=issues,
            has_code=bool(real_usages),
            has_doc=bool(doc_items),
        )

        impacted_paths = {str(i.meta.get("path", "")) for i in real_usages}
        tests = recommend_tests(self._code_report, impacted_paths, repo_root=self._root)

        score, severity, factors = score_risk(
            RiskScoringInput(
                status=status,
                code_items=code_items,
                doc_items=doc_items,
                skill=self._skill,
                source_version_spec=report.source_version_spec,
                target_version_spec=report.target_version_spec,
                has_recommended_tests=bool(tests),
                risk_title=title,
            )
        )

        return VerifiedRisk(
            risk_id=risk_id,
            title=title,
            status=status,
            severity=severity,
            model_severity=model_severity,
            rule_score=score,
            factors=factors,
            code_evidence_ids=[i.evidence_id for i in code_items],
            doc_evidence_ids=[i.evidence_id for i in doc_items],
            unknown_evidence_ids=unknown,
            issues=issues,
            recommended_tests=tests,
            recommendation=recommendation,
        )

    def _conclude(self, verified: list[VerifiedRisk], degraded: list[VerifiedRisk]) -> Conclusion:
        """Decide the headline answer.

        Order matters. A confirmed risk always means "impacted", even when it
        only affects test code -- reporting "no impact" alongside a verified
        risk would be self-contradictory.

        Beyond that, "no impact" requires the *absence of usage*, not merely the
        absence of confirmed risks; otherwise a failed analysis would look like
        a clean bill of health.
        """
        if verified:
            return Conclusion.IMPACTED
        production_usages = [
            item
            for item in self._bundle.by_kind("code_usage")
            if not item.meta.get("is_test_code", False)
        ]
        if not production_usages and not self._bundle.by_kind("dynamic_import"):
            return Conclusion.NO_IMPACT
        if degraded:
            return Conclusion.EVIDENCE_INSUFFICIENT
        return Conclusion.NO_IMPACT


def verify_report(
    report: ImpactReport,
    *,
    repo_root: Path,
    bundle: EvidenceBundle,
    code_report: CodeEvidenceReport,
    skill: SkillPackage | None = None,
    degradations: list[str] | None = None,
) -> VerifiedReport:
    """Convenience wrapper around :class:`EvidenceVerifier`."""
    verifier = EvidenceVerifier(
        repo_root=repo_root,
        bundle=bundle,
        code_report=code_report,
        skill=skill,
    )
    return verifier.verify(report, degradations=degradations)
