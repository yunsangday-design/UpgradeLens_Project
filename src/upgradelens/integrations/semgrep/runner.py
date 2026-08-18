"""Semgrep runner: orchestrate the offline scanner or the real CLI invocation.

Real mode uses an argument-array ``subprocess`` call (never shell
interpolation), restricts rule sets to :data:`ALLOWED_CONFIGS`, enforces a
timeout and an output-size cap, and degrades clearly when ``semgrep`` is not
installed instead of silently producing wrong results.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .adapter import _fake_scan, _parse_sarif
from .models import (
    ALLOWED_CONFIGS,
    MAX_OUTPUT_BYTES,
    SUPPORTED_SEMGREP_VERSION,
    SemgrepResult,
)


def check_semgrep_available() -> bool:
    """Return ``True`` if the ``semgrep`` CLI is installed and runnable."""
    try:
        proc = subprocess.run(
            ["semgrep", "--version"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def run_semgrep(
    repo_root: str | Path,
    *,
    fake: bool = True,
    config: str | None = None,
    timeout: float = 120.0,
) -> SemgrepResult:
    """Run semgrep over ``repo_root``.

    When ``fake`` is True (the default for offline runs) the deterministic regex
    scanner is used. Otherwise the ``semgrep`` CLI is invoked and its SARIF
    parsed. Only rule sets listed in :data:`ALLOWED_CONFIGS` are permitted.
    """
    if fake:
        return SemgrepResult(findings=_fake_scan(repo_root), used_fake=True)
    if config is not None and config not in ALLOWED_CONFIGS:
        raise ValueError(f"semgrep config {config!r} not allowed; use one of {ALLOWED_CONFIGS}")
    if not check_semgrep_available():
        raise RuntimeError(
            "semgrep CLI is not available; the security-review capability "
            f"requires semgrep {SUPPORTED_SEMGREP_VERSION}. Pass fake=True to run "
            "the offline scanner."
        )
    cmd = ["semgrep", "--sarif", "--config", config or "auto", str(repo_root)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    stdout = proc.stdout or ""
    if len(stdout.encode("utf-8", "ignore")) > MAX_OUTPUT_BYTES:
        raise RuntimeError("semgrep output exceeded the size limit")
    sarif = json.loads(stdout or "{}")
    return SemgrepResult(findings=_parse_sarif(sarif), sarif=sarif, used_fake=False)
