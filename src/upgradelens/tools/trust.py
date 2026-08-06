"""Infer a documentation source's trust level from its URL.

Trust drives how much the Verifier is allowed to lean on a piece of evidence
(stage 6). The rule is a small, auditable allow-list -- exactly the kind of
thing a reviewer can eyeball in seconds.
"""

from __future__ import annotations

from urllib.parse import urlparse

from upgradelens.domain.skill import TrustLevel

#: Hosts we treat as first-party / canonical for the ecosystems we care about.
OFFICIAL_HOSTS = frozenset(
    {
        "pypi.org",
        "github.com",
        "raw.githubusercontent.com",
        "docs.python.org",
        "peps.python.org",
    }
)

#: Community-maintained but commonly authoritative hosts.
COMMUNITY_HOSTS = frozenset(
    {
        "github.io",
        "readthedocs.io",
        "readthedocs.org",
        "gitlab.io",
    }
)


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def infer_trust(url: str) -> TrustLevel:
    """Map a documentation URL to a :class:`TrustLevel`.

    ``official`` for first-party hosts, ``community`` for well-known community
    doc hosts, otherwise ``unverified``. The mapping is deliberately optimistic
    only for hosts we explicitly recognise -- anything we have not vetted stays
    unverified so the Verifier discounts it.
    """
    host = _host_of(url)
    if host in OFFICIAL_HOSTS:
        return "official"
    for suffix in COMMUNITY_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return "community"
    return "unverified"


def trust_for_url(url: str) -> TrustLevel:
    """Public alias for :func:`infer_trust`."""
    return infer_trust(url)
