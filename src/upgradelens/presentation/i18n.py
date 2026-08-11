"""S14: locale-aware labels for user-facing enum/status values.

The backend keeps machine-readable enum values (English snake_case) inside the
JSON contracts so callers can branch on them programmatically. But the demo UI,
markdown reports and any external Coding Agent should *display* Chinese labels
without re-deriving them in JavaScript.

Every function takes a ``locale`` and defaults to ``"zh-CN"``. English is provided
as a fallback dictionary for the few locales we support; unknown values fall back
to the raw input so the contract never breaks.
"""
from __future__ import annotations

from enum import StrEnum

DEFAULT_LOCALE = "zh-CN"

_VERDICT_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "evidence_insufficient": "证据不足，无法给出定论",
        "needs_upgrade": "需要升级（存在已验证/已降级的破坏性变更）",
        "no_risk": "无需升级（代码未触及破坏性面）",
        "no_impact": "无影响（未发现破坏性行为）",
    },
    "en": {
        "evidence_insufficient": "Insufficient evidence",
        "needs_upgrade": "Upgrade required",
        "no_risk": "No risk",
        "no_impact": "No impact",
    },
}

_CONCLUSION_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "impacted": "存在破坏性变更",
        "no_impact": "无破坏性影响",
        "evidence_insufficient": "证据不足",
    },
    "en": {
        "impacted": "Impacted",
        "no_impact": "No impact",
        "evidence_insufficient": "Insufficient evidence",
    },
}

_SEVERITY_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {"high": "高", "medium": "中", "low": "低", "": "低"},
    "en": {"high": "High", "medium": "Medium", "low": "Low", "": "Low"},
}

_EVIDENCE_STATUS_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "verified": "已验证",
        "partially_verified": "部分验证",
        "insufficient_evidence": "证据不足",
        "conflicting_evidence": "证据冲突",
        "not_applicable": "不适用",
    },
    "en": {
        "verified": "Verified",
        "partially_verified": "Partially verified",
        "insufficient_evidence": "Insufficient evidence",
        "conflicting_evidence": "Conflicting evidence",
        "not_applicable": "Not applicable",
    },
}

_PLAN_MODE_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "patch_draft": "补丁草稿",
        "sandbox_apply": "沙箱应用",
    },
    "en": {
        "patch_draft": "Patch draft",
        "sandbox_apply": "Sandbox apply",
    },
}

_MODEL_MODE_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {"fake": "离线模拟", "replay": "回放", "live": "在线"},
    "en": {"fake": "Fake", "replay": "Replay", "live": "Live"},
}

_EXECUTION_STATUS_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "accepted": "已采纳",
        "rejected": "已拒绝",
        "needs_review": "需复核",
        "skipped": "已跳过",
    },
    "en": {
        "accepted": "Accepted",
        "rejected": "Rejected",
        "needs_review": "Needs review",
        "skipped": "Skipped",
    },
}

_TRUST_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "official": "官方",
        "community": "社区",
        "unverified": "未验证",
    },
    "en": {
        "official": "Official",
        "community": "Community",
        "unverified": "Unverified",
    },
}

_ISSUE_CODE_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "no_evidence_ids": "未提供证据编号",
        "unknown_evidence_id": "未知证据编号",
        "no_code_evidence": "缺少代码证据",
        "file_not_found": "文件未找到",
        "line_out_of_range": "行号超出范围",
        "content_hash_changed": "内容哈希已变化",
        "no_doc_evidence": "缺少文档证据",
        "doc_version_conflict": "文档版本冲突",
        "doc_source_untrusted": "文档来源不可信",
        "dynamic_only_evidence": "仅动态证据",
        "symbol_not_in_evidence": "符号未落到证据",
        "unknown_test_path": "测试路径未知",
    },
    "en": {
        "no_evidence_ids": "No evidence ids",
        "unknown_evidence_id": "Unknown evidence id",
        "no_code_evidence": "No code evidence",
        "file_not_found": "File not found",
        "line_out_of_range": "Line out of range",
        "content_hash_changed": "Content hash changed",
        "no_doc_evidence": "No doc evidence",
        "doc_version_conflict": "Doc version conflict",
        "doc_source_untrusted": "Untrusted doc source",
        "dynamic_only_evidence": "Dynamic-only evidence",
        "symbol_not_in_evidence": "Symbol not in evidence",
        "unknown_test_path": "Unknown test path",
    },
}

_DEGRADATION_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "corpus_miss": "语料缺失",
        "corpus_stale": "语料过期",
        "doc_unavailable": "文档不可用",
        "retrieval_failed": "检索失败",
        "mode_fallback": "模式回退",
    },
    "en": {
        "corpus_miss": "Corpus missing",
        "corpus_stale": "Corpus stale",
        "doc_unavailable": "Doc unavailable",
        "retrieval_failed": "Retrieval failed",
        "mode_fallback": "Mode fallback",
    },
}


def _pick(table: dict[str, dict[str, str]], value: str, locale: str) -> str:
    by_locale = table.get(locale, table.get(DEFAULT_LOCALE, {}))
    if value in by_locale:
        return by_locale[value]
    # Fall back to the zh-CN table, then to the raw value.
    return table.get(DEFAULT_LOCALE, {}).get(value, value)


def verdict_label(verdict: str, locale: str = DEFAULT_LOCALE) -> str:
    return _pick(_VERDICT_LABELS, verdict, locale)


def conclusion_label(conclusion: StrEnum | str, locale: str = DEFAULT_LOCALE) -> str:
    value = conclusion.value if isinstance(conclusion, StrEnum) else str(conclusion)
    return _pick(_CONCLUSION_LABELS, value, locale)


def severity_label(severity: str, locale: str = DEFAULT_LOCALE) -> str:
    return _pick(_SEVERITY_LABELS, severity or "", locale)


def evidence_status_label(status: StrEnum | str, locale: str = DEFAULT_LOCALE) -> str:
    value = status.value if isinstance(status, StrEnum) else str(status)
    return _pick(_EVIDENCE_STATUS_LABELS, value, locale)


def plan_mode_label(mode: StrEnum | str, locale: str = DEFAULT_LOCALE) -> str:
    value = mode.value if isinstance(mode, StrEnum) else str(mode)
    return _pick(_PLAN_MODE_LABELS, value, locale)


def model_mode_label(mode: StrEnum | str, locale: str = DEFAULT_LOCALE) -> str:
    value = mode.value if isinstance(mode, StrEnum) else str(mode)
    return _pick(_MODEL_MODE_LABELS, value, locale)


def execution_status_label(status: StrEnum | str, locale: str = DEFAULT_LOCALE) -> str:
    value = status.value if isinstance(status, StrEnum) else str(status)
    return _pick(_EXECUTION_STATUS_LABELS, value, locale)


def trust_label(level: str, locale: str = DEFAULT_LOCALE) -> str:
    return _pick(_TRUST_LABELS, level or "", locale)


def issue_code_label(code: StrEnum | str, locale: str = DEFAULT_LOCALE) -> str:
    value = code.value if isinstance(code, StrEnum) else str(code)
    return _pick(_ISSUE_CODE_LABELS, value, locale)


def degradation_label(code: str, locale: str = DEFAULT_LOCALE) -> str:
    return _pick(_DEGRADATION_LABELS, code, locale)


def localize(value: object, locale: str = DEFAULT_LOCALE) -> str:
    """Best-effort dispatcher for the common enum/status types."""
    if isinstance(value, StrEnum):
        raw = value.value
    else:
        raw = str(value)
    for table in (
        _CONCLUSION_LABELS,
        _VERDICT_LABELS,
        _SEVERITY_LABELS,
        _EVIDENCE_STATUS_LABELS,
        _PLAN_MODE_LABELS,
        _MODEL_MODE_LABELS,
        _EXECUTION_STATUS_LABELS,
        _TRUST_LABELS,
        _ISSUE_CODE_LABELS,
    ):
        if raw in table.get(locale, {}) or raw in table.get(DEFAULT_LOCALE, {}):
            return _pick(table, raw, locale)
    return raw
