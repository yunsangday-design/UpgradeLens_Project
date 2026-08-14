"""Intent routing: classify a free-text request and extract the three elements.

This is the thin natural-language front door described in ``ROADMAP.md`` Step 1
(A2). It does **not** run an assessment -- it produces an
:class:`Intent`, the exact three-element shape that ``AssessmentRequest``
consumes, so the shared pipeline (Step 0.1) can be driven without flags.

Design, in order of precedence:

1. **URL gate runs before any model call.** A GitHub URL found in the text is
   validated (scheme, GitHub host, not an internal address, parseable slug)
   and a bad one returns ``invalid_url`` immediately. No token is spent.
2. **Rule extraction** then pulls repo / dependency / target_version out of the
   text with regexes and the built-in Skill Pack names. It is what makes the
   ``fake`` run mode fully offline and deterministic.
3. **Only when a live gateway is present** does the model refine the extraction
   (mainly the dependency and version). The validated repo always wins over
   anything the model might invent, so the SSRF gate still holds.

``not_upgrade`` requests (chit-chat, no repo, no package, no task keyword) are
short-circuited with no model call either, so they cost nothing.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Literal
from urllib.parse import urlparse

from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

from upgradelens.llm.gateway import ModelGateway, ModelMode
from upgradelens.llm.prompts import get_prompt
from upgradelens.skills import builtin_registry
from upgradelens.tools.errors import OutOfNetworkError
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.live_repo import parse_repo_slug

IntentKind = Literal[
    "upgrade_task", "scan_upgradable", "not_upgrade", "invalid_url", "need_clarification"
]

# Words that signal the user wants to scan ALL dependencies for upgrades.
_SCAN_KEYWORDS = (
    "扫描依赖",
    "扫描所有依赖",
    "检查所有依赖",
    "哪些可以升级",
    "哪些能升级",
    "scan dependencies",
    "scan upgradable",
    "check all dependencies",
    "which can be upgraded",
)

# Words that signal the user wants *some* repository analysis but has not given
# enough to run it. Distinct from an explicit upgrade keyword.
_TASK_KEYWORDS = (
    "看看",
    "看下",
    "分析",
    "评估",
    "检查",
    "审查",
    "帮我",
    "这个仓库",
    "这个代码",
    "review",
    "check",
    "analyse",
    "analyze",
    "assess",
    "inspect",
    "examine",
    "scan",
)
# Words that signal a version upgrade specifically.
_UPGRADE_KEYWORDS = (
    "升级",
    "升到",
    "升",
    "迁移",
    "升级到",
    "migrate",
    "upgrade",
    "bump",
    "update",
)

# Tokens that must never be mistaken for a dependency name.
_STOPWORDS = {
    "github",
    "com",
    "http",
    "https",
    "www",
    "the",
    "this",
    "repo",
    "repository",
    "to",
    "from",
    "version",
    "upgrade",
    "migrate",
    "up",
    "of",
    "a",
    "an",
    "and",
}


def _default_known_deps() -> set[str]:
    """Built-in Skill Pack names become the known-dependency allow-list.

    Falls back to empty on any registry error so routing never breaks the
    front door -- unknown names simply fall through to the plausible-token rule.
    """
    names: set[str] = set()
    try:
        for skill in builtin_registry().all():
            for name in skill.package_names:
                if name != "*":
                    names.add(canonicalize_name(name))
    except Exception:
        return names
    return names


_URL_RE = re.compile(r"https?://[^\s)\]>'\"，。]+")
# A plausible Python package name token: starts with a letter, word chars / . / -
_PKG_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_VERSION_TOKEN_RE = re.compile(r"[0-9][0-9A-Za-z.*+~-]*")
# "from X to Y" or "X -> Y" : capture current and target versions.
_RANGE_RE = re.compile(
    r"(?:from\s+)?([0-9][0-9A-Za-z.*+~-]*)\s*(?:to|->|=>|升(?:级)?到)\s*([0-9][0-9A-Za-z.*+~-]*)"
)
# A target version following an upgrade cue word.
_TARGET_RE = re.compile(r"(?:升(?:级)?到|迁移到|升级到|to|->|=>|版本)\s*([0-9][0-9A-Za-z.*+~-]*)")


class Intent(BaseModel):
    """Structured outcome of routing a free-text request.

    ``kind`` decides what the caller does next: hand the three elements to the
    pipeline (``upgrade_task``), ask the user for the missing pieces
    (``need_clarification``), ignore it (``not_upgrade``), or refuse outright
    (``invalid_url``).
    """

    kind: IntentKind
    repo: str | None = None
    dependency: str | None = None
    target_version: str | None = None
    source_version: str | None = None
    missing: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    clarification: str | None = None


def _is_github_host(host: str) -> bool:
    return host == "github.com" or host.endswith(".github.com")


def _is_internal_literal(host: str) -> bool:
    """True for a literal address that points at a private/loopback network."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def _validate_url(url: str, fetcher: RestrictedFetcher | None) -> tuple[bool, str]:
    """Return ``(ok, reason)``.

    The SSRF guard reuses :meth:`RestrictedFetcher.is_url_allowed` when a fetcher
    is supplied; for non-literal hosts that performs a DNS resolution, so callers
    that need a fully offline check should pass ``fetcher=None`` and rely on the
    GitHub-host + literal-IP checks (still safe for internal addresses).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "only http(s) repository URLs are supported"
    host = parsed.hostname or ""
    if not host:
        return False, "the repository URL has no host"
    if not _is_github_host(host):
        return False, "only github.com repository URLs are supported"
    if _is_internal_literal(host):
        return False, "the repository URL points at an internal address"
    if fetcher is not None and not fetcher.is_url_allowed(url):
        return False, "the repository URL is not allowed"
    try:
        parse_repo_slug(url)
    except OutOfNetworkError as exc:
        return False, f"cannot parse the GitHub owner/repo from the URL: {exc}"
    return True, ""


def _extract_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(".,;:")


def _strip_url(text: str) -> str:
    return _URL_RE.sub(" ", text)


def _extract_dependency(text: str, known: set[str]) -> str | None:
    body = _strip_url(text)
    lowered = body.lower()
    # Known dependencies first -- an exact case-insensitive word match wins.
    for name in known:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", lowered):
            return name
    # Otherwise accept the first plausible package-name token.
    tokens: list[str] = _PKG_TOKEN_RE.findall(body)
    for token in tokens:
        if len(token) < 2:
            continue
        if token.lower() in _STOPWORDS:
            continue
        if _VERSION_TOKEN_RE.fullmatch(token):
            continue
        return token.lower()
    return None


def _extract_versions(text: str) -> tuple[str | None, str | None]:
    body = _strip_url(text)
    range_match = _RANGE_RE.search(body)
    if range_match is not None:
        return range_match.group(2), range_match.group(1)
    target_match = _TARGET_RE.search(body)
    if target_match is not None:
        return target_match.group(1), None
    return None, None


def _has_keyword(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _clarification(missing: list[str], repo: str | None) -> str:
    labels = {
        "repo": "the repository (a github.com URL or local path)",
        "dependency": "the dependency being upgraded",
        "target_version": "the target version to upgrade to",
    }
    needed = ", ".join(labels[m] for m in missing)
    base = f"To assess the upgrade I need: {needed}."
    if repo is not None:
        base += " The repository is already known."
    return base


class Router:
    """Turn free text into an :class:`Intent`.

    ``gateway`` drives the optional LLM refinement (skipped in ``fake`` mode);
    ``fetcher`` supplies the SSRF guard for the URL gate. ``known_dependencies``
    overrides the built-in Skill Pack names used by the rule extractor.
    """

    def __init__(
        self,
        *,
        gateway: ModelGateway | None = None,
        fetcher: RestrictedFetcher | None = None,
        known_dependencies: Iterable[str] | None = None,
    ) -> None:
        self._gateway = gateway
        self._fetcher = fetcher
        self._known = (
            set(known_dependencies) if known_dependencies is not None else _default_known_deps()
        )

    def route(self, text: str) -> Intent:
        url = _extract_url(text)
        if url is not None:
            ok, reason = _validate_url(url, self._fetcher)
            if not ok:
                return Intent(kind="invalid_url", repo=url, confidence=1.0, clarification=reason)

        repo = url
        dependency = _extract_dependency(text, self._known)
        target, source = _extract_versions(text)

        if self._use_llm():
            llm = self._llm_intent(text)
            repo = repo or self._validated_repo(llm.repo)
            dependency = llm.dependency or dependency
            target = llm.target_version or target
            source = llm.source_version or source

        # Check for scan intent before general assembly
        if _has_keyword(text, _SCAN_KEYWORDS) and repo is not None:
            return Intent(
                kind="scan_upgradable",
                repo=repo,
                confidence=0.9,
            )

        return self._assemble(repo, dependency, target, source, text)

    def _use_llm(self) -> bool:
        return self._gateway is not None and self._gateway.mode != ModelMode.FAKE

    def _llm_intent(self, text: str) -> Intent:
        assert self._gateway is not None
        prompt = get_prompt("router").render(user_text=text)
        intent, _ = self._gateway.complete_structured(prompt=prompt, schema=Intent, name="router")
        return intent

    def _validated_repo(self, candidate: str | None) -> str | None:
        if not candidate:
            return None
        ok, _ = _validate_url(candidate, self._fetcher)
        return candidate if ok else None

    def _assemble(
        self,
        repo: str | None,
        dependency: str | None,
        target: str | None,
        source: str | None,
        text: str,
    ) -> Intent:
        elements = {
            "repo": repo,
            "dependency": dependency,
            "target_version": target,
        }
        missing = [name for name, value in elements.items() if value is None]

        if not missing:
            return Intent(
                kind="upgrade_task",
                repo=repo,
                dependency=dependency,
                target_version=target,
                source_version=source,
                confidence=0.9,
            )

        wants_task = (
            repo is not None
            or _has_keyword(text, _TASK_KEYWORDS)
            or _has_keyword(text, _UPGRADE_KEYWORDS)
        )
        if wants_task:
            return Intent(
                kind="need_clarification",
                repo=repo,
                dependency=dependency,
                target_version=target,
                source_version=source,
                missing=missing,
                confidence=0.6,
                clarification=_clarification(missing, repo),
            )
        return Intent(kind="not_upgrade", confidence=0.9)


def route(text: str, *, gateway: ModelGateway | None = None) -> Intent:
    """Convenience wrapper: route ``text`` with an optional live ``gateway``."""
    return Router(gateway=gateway).route(text)
