"""Loose version extraction helpers used by documentation conflict checks.

Skill metadata and CLI arguments carry human-friendly specifiers such as
``">=2.0"``, ``"2.7.0"`` or ``">=2,<3"``. For conflict detection we only need a
single representative version, and we must never raise on odd input.
"""

from __future__ import annotations

import re

__all__ = ["extract_version"]

_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def extract_version(spec: str) -> str | None:
    """Return the first dotted version found in ``spec``, or ``None``.

    ``">=2.0"`` -> ``"2.0"``; ``"pydantic"`` -> ``None``.
    """
    if not spec:
        return None
    match = _VERSION_RE.search(spec)
    if match is None:
        return None
    return match.group(0)
