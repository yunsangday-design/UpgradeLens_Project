"""Rule-driven patch draft generator (plan section 2.3).

Given the *verified* risks and the repository, this produces Unified Diff hunks
for mechanical rewrites the skill knows about (e.g. ``@validator`` -> ``
@field_validator``, ``.dict()`` -> ``.model_dump()``).

Safety invariants (these are the whole point of stage 8):

* it only ever looks at files already referenced by a *verified* code evidence
  -- never arbitrary files in the repo;
* it only rewrites a line when the skill's rule pattern actually matches there;
* it returns a diff string and **never writes back to the working tree**;
* rules that need a quality model, or carry ``high`` patch risk, are recorded
  as *skipped* unless a quality model is in play.
"""

from __future__ import annotations

import re
from pathlib import Path

from upgradelens.domain.skill import PatchRule, SkillPackage
from upgradelens.models.impact import EvidenceBundle
from upgradelens.patch.models import PatchDraft, PatchFileDiff, PatchHunk
from upgradelens.verify.models import VerifiedRisk

_CONTEXT = 3


def _apply_rule(rule: PatchRule, line: str) -> str:
    """Return ``line`` after one application of ``rule`` (or ``line`` unchanged)."""
    if rule.target_regex and rule.replacement is not None:
        return re.sub(rule.target_regex, rule.replacement, line, count=1)
    return line.replace(rule.target_pattern, rule.replacement_template, 1)


def _matches_for_file(
    repo_root: Path,
    risks: list[VerifiedRisk],
    skill: SkillPackage,
    bundle: EvidenceBundle,
    *,
    quality_model_available: bool,
) -> dict[str, list[tuple[int, str, str, str]]]:
    """Collect (line_index, old, new, rule_id) per file from verified evidence."""
    by_file: dict[str, list[tuple[int, str, str, str]]] = {}
    applied: set[tuple[str, str]] = set()  # (path, rule_id) -> at most one hunk each

    for risk in risks:
        if risk.status != "verified":
            continue
        for eid in risk.code_evidence_ids:
            item = bundle.get(eid)
            if item is None or item.kind != "code_usage":
                continue
            path = item.meta.get("path")
            line = item.meta.get("line")
            if path is None or line is None:
                continue
            abs_path = repo_root / path
            if not abs_path.is_file():
                continue
            try:
                lines = abs_path.read_text(encoding="utf-8").split("\n")
            except OSError:
                continue
            target = max(0, int(line) - 1)  # 1-based -> 0-based
            lo = max(0, target - 4)
            hi = min(len(lines), target + 5)
            for rule in skill.patch_rules:
                if (path, rule.id) in applied:
                    continue
                if rule.requires_quality_model and not quality_model_available:
                    continue
                if rule.patch_risk_level == "high" and not quality_model_available:
                    continue
                for idx in range(lo, hi):
                    old = lines[idx]
                    new = _apply_rule(rule, old)
                    if new != old:
                        by_file.setdefault(path, []).append((idx, old, new, rule.id))
                        applied.add((path, rule.id))
                        break
    return by_file


def _build_file_diff(
    path: str, matches: list[tuple[int, str, str, str]], repo_root: Path
) -> PatchFileDiff | None:
    if not matches:
        return None
    abs_path = repo_root / path
    try:
        lines = abs_path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return None

    matches_sorted = sorted(matches, key=lambda m: m[0])
    lo = max(0, matches_sorted[0][0] - _CONTEXT)
    hi = min(len(lines), matches_sorted[-1][0] + _CONTEXT + 1)

    new_lines = list(lines[lo:hi])
    for idx, _old, new, _rule in matches_sorted:
        new_lines[idx - lo] = new
    body: list[str] = []
    for j, text in enumerate(new_lines):
        orig = lines[lo + j]
        if text == orig:
            body.append(" " + orig)
        else:
            body.append("-" + orig)
            body.append("+" + text)

    old_count = hi - lo
    new_count = len(new_lines)
    hunk = PatchHunk(
        path=path,
        old_start=lo + 1,
        old_count=old_count,
        new_start=lo + 1,
        new_count=new_count,
        body=body,
    )
    diff_text = (
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -{lo + 1},{old_count} +{lo + 1},{new_count} @@\n" + "\n".join(body)
    )
    return PatchFileDiff(path=path, hunks=[hunk], diff_text=diff_text)


def generate_patch_draft(
    repo_root: Path,
    verified_risks: list[VerifiedRisk],
    skill: SkillPackage,
    bundle: EvidenceBundle,
    *,
    quality_model_available: bool = False,
) -> PatchDraft:
    """Build a review-ready patch draft from verified evidence + skill rules.

    The function is pure: it reads the working tree to locate matches but never
    mutates it. Returns an (often empty) :class:`PatchDraft`.
    """
    draft = PatchDraft(
        dependency=next(iter(skill.package_names), ""),
        target_version_spec=skill.target_version_spec or "",
        skill_id=skill.skill_id,
        allow_patch_draft=skill.allow_patch_draft,
        quality_model_available=quality_model_available,
    )
    if not skill.allow_patch_draft:
        draft.notes = "Skill does not permit patch drafts; none generated."
        return draft

    by_file = _matches_for_file(
        Path(repo_root),
        verified_risks,
        skill,
        bundle,
        quality_model_available=quality_model_available,
    )
    applied: set[str] = set()
    skipped: set[str] = set()
    for rule in skill.patch_rules:
        eligible = not (
            (rule.requires_quality_model and not quality_model_available)
            or (rule.patch_risk_level == "high" and not quality_model_available)
        )
        used = any(rule.id in {m[3] for m in matches} for matches in by_file.values())
        if eligible and used:
            applied.add(rule.id)
        elif not eligible and used:
            skipped.add(rule.id)

    files: list[PatchFileDiff] = []
    for path, matches in by_file.items():
        fd = _build_file_diff(path, matches, Path(repo_root))
        if fd is not None:
            files.append(fd)

    draft.files = files
    draft.applied_rules = sorted(applied)
    draft.skipped_rules = sorted(skipped)
    return draft
