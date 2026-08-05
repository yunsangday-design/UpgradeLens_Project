"""Cross-platform path and text helpers.

All paths written to output (JSON, reports, logs) must be POSIX-style relative
paths so results are identical on macOS, Windows and Linux containers. Machine
absolute paths such as ``/Users/...`` or ``C:\\Users\\...`` must never appear in
fixture expectations or emitted results.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["read_text_utf8", "to_posix_rel_path"]


def to_posix_rel_path(repo_root: Path, target: Path) -> str:
    """Return ``target`` as a POSIX relative path from ``repo_root``.

    Accepts both absolute and already-relative ``target``. The result always
    uses ``/`` as separator so JSON output is byte-identical on macOS and
    Windows.

    Raises:
        ValueError: if ``target`` is not located under ``repo_root``.
    """
    root = Path(repo_root).resolve()
    candidate = Path(target)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    return resolved.relative_to(root).as_posix()


def read_text_utf8(path: Path) -> str:
    """Read a text file as UTF-8 using universal newlines.

    ``CRLF`` and ``LF`` inputs both yield ``\\n`` line endings, so parsers and
    line numbers behave identically regardless of the platform that produced
    the file.
    """
    return Path(path).read_text(encoding="utf-8")
