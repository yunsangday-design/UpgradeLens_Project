"""Test Profile: identify pytest configuration, test directories and naming (S8).

Deterministic and offline: scans the repository for existing test locations and
reads the pytest configuration (``pyproject.toml`` / ``pytest.ini``) so the rest
of the test-intelligence pipeline can recommend and generate tests that match the
project's conventions.
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.repository.scan import scan_repository

__all__ = ["PytestConfig", "TestProfileInfo", "build_test_profile"]


class PytestConfig(BaseModel):
    """Parsed pytest configuration."""

    model_config = ConfigDict(frozen=True)

    rootdir: str = ""
    testpaths: list[str] = Field(default_factory=list)
    python_files: list[str] = Field(default_factory=list)
    addopts: str = ""


class TestProfileInfo(BaseModel):
    """Combined static test profile for a repository."""

    model_config = ConfigDict(frozen=True)

    test_paths: list[str] = []
    framework: str = ""
    pytest: PytestConfig = PytestConfig()


def _detect_pytest_config(root: Path) -> PytestConfig:
    testpaths: list[str] = []
    python_files: list[str] = []
    addopts = ""

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except tomllib.TOMLDecodeError:
            data = {}
        tool = data.get("tool", {}) or {}
        pytest_cfg = {
            **(tool.get("pytest", {}) or {}),
            **(tool.get("pytest.ini_options", {}) or {}),
        }
        if not testpaths:
            tp = pytest_cfg.get("testpaths")
            if tp:
                testpaths = list(tp) if isinstance(tp, list) else str(tp).split()
        if not python_files:
            pf = pytest_cfg.get("python_files")
            if pf:
                python_files = pf.split() if isinstance(pf, str) else list(pf)
        if not addopts:
            ao = pytest_cfg.get("addopts")
            if ao:
                addopts = ao if isinstance(ao, str) else " ".join(ao)

    ini = root / "pytest.ini"
    if ini.is_file():
        cp = configparser.ConfigParser()
        try:
            cp.read_string(ini.read_text(encoding="utf-8", errors="replace"))
        except configparser.Error:
            cp = configparser.ConfigParser()
        if cp.has_section("pytest"):
            if not testpaths:
                testpaths = [v for v in cp.get("pytest", "testpaths", fallback="").split() if v]
            if not addopts:
                addopts = cp.get("pytest", "addopts", fallback="")

    return PytestConfig(
        rootdir=str(root),
        testpaths=testpaths,
        python_files=python_files,
        addopts=addopts,
    )


def build_test_profile(repo_root: str | Path) -> TestProfileInfo:
    """Build a :class:`TestProfileInfo` from ``repo_root`` (offline, deterministic)."""
    profile = scan_repository(Path(repo_root))
    return TestProfileInfo(
        test_paths=list(profile.tests.test_paths),
        framework=profile.tests.framework,
        pytest=_detect_pytest_config(Path(repo_root)),
    )
