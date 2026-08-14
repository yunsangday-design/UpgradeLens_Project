"""Tests for step12 increment D: MVP upgradable dependency scan.

D.1: Full-manifest parsers (parse_all_requirements_txt / parse_all_pyproject_toml)
D.2: PyPIClient.latest_stable_version (filters pre-release/yanked)
D.3: scan_upgradable_dependencies (end-to-end scan logic)
D.4: Router scan_upgradable intent
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from upgradelens.agent.router import Router
from upgradelens.analyzers.manifests import (
    parse_all_pyproject_toml,
    parse_all_requirements_txt,
)
from upgradelens.analyzers.upgradable_scan import (
    UpgradableScanResult,
    scan_upgradable_dependencies,
)
from upgradelens.tools.pypi import PyPIClient

# ---------------------------------------------------------------------------
# D.1: Full-manifest parsers
# ---------------------------------------------------------------------------


class TestParseAllRequirementsTxt:
    """parse_all_requirements_txt returns ALL deps, not filtered."""

    def test_multiple_deps(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.31.0\npydantic>=2.0,<3.0\nflask\n")
        outcome = parse_all_requirements_txt(tmp_path, req)
        names = [d.raw_name for d in outcome.declarations]
        assert "requests" in names
        assert "pydantic" in names
        assert "flask" in names
        assert len(outcome.declarations) == 3

    def test_skips_options_and_urls(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text("-r other.txt\nhttps://example.com/pkg.tar.gz\nrequests==1.0\n")
        outcome = parse_all_requirements_txt(tmp_path, req)
        assert len(outcome.declarations) == 1
        assert outcome.declarations[0].raw_name == "requests"
        assert len(outcome.warnings) >= 2

    def test_invalid_line(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text("valid-pkg==1.0\n!!!invalid\n")
        outcome = parse_all_requirements_txt(tmp_path, req)
        assert len(outcome.declarations) == 1
        assert len(outcome.errors) == 1

    def test_marker_warning(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text('pywin32==306; sys_platform == "win32"\n')
        outcome = parse_all_requirements_txt(tmp_path, req)
        assert len(outcome.declarations) == 1
        assert outcome.declarations[0].marker is not None
        assert len(outcome.warnings) == 1


class TestParseAllPyprojectToml:
    """parse_all_pyproject_toml returns ALL deps from [project].dependencies."""

    def test_multiple_deps(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\n'
            'dependencies = ["requests>=2.28", "pydantic==2.5.0", "click"]\n'
        )
        outcome = parse_all_pyproject_toml(tmp_path, pyproject)
        names = [d.raw_name for d in outcome.declarations]
        assert "requests" in names
        assert "pydantic" in names
        assert "click" in names
        assert len(outcome.declarations) == 3

    def test_missing_project_table(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.poetry]\nname = "test"\n')
        outcome = parse_all_pyproject_toml(tmp_path, pyproject)
        assert len(outcome.declarations) == 0
        assert len(outcome.warnings) == 1

    def test_dynamic_dependencies(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\ndynamic = ["dependencies"]\n'
        )
        outcome = parse_all_pyproject_toml(tmp_path, pyproject)
        assert len(outcome.declarations) == 0
        assert len(outcome.warnings) == 1


# ---------------------------------------------------------------------------
# D.2: PyPIClient.latest_stable_version
# ---------------------------------------------------------------------------


class TestLatestStableVersion:
    """PyPIClient.latest_stable_version filters pre-release and yanked."""

    def _mock_client(self, releases: dict[str, list[dict[str, Any]]]) -> PyPIClient:
        fetcher = MagicMock()
        client = PyPIClient(fetcher)
        data = {"info": {"version": "0.0.0"}, "releases": releases}
        fetcher.fetch.return_value = MagicMock(
            content=json.dumps(data).encode("utf-8")
        )
        return client

    def test_returns_latest_stable(self):
        client = self._mock_client({
            "1.0.0": [{"yanked": False}],
            "2.0.0": [{"yanked": False}],
            "2.1.0a1": [{"yanked": False}],  # pre-release
        })
        assert client.latest_stable_version("pkg") == "2.0.0"

    def test_skips_yanked(self):
        client = self._mock_client({
            "1.0.0": [{"yanked": False}],
            "2.0.0": [{"yanked": True}],  # fully yanked
        })
        assert client.latest_stable_version("pkg") == "1.0.0"

    def test_partially_yanked_not_excluded(self):
        """If not ALL files are yanked, the release is NOT excluded."""
        client = self._mock_client({
            "1.0.0": [{"yanked": False}],
            "2.0.0": [{"yanked": True}, {"yanked": False}],
        })
        assert client.latest_stable_version("pkg") == "2.0.0"

    def test_no_stable_returns_none(self):
        client = self._mock_client({
            "1.0.0a1": [{"yanked": False}],
            "2.0.0.dev1": [{"yanked": False}],
        })
        assert client.latest_stable_version("pkg") is None

    def test_empty_releases(self):
        client = self._mock_client({})
        assert client.latest_stable_version("pkg") is None


# ---------------------------------------------------------------------------
# D.3: scan_upgradable_dependencies
# ---------------------------------------------------------------------------


class TestScanUpgradableDependencies:
    """End-to-end scan with mocked PyPI responses."""

    def _setup_repo(self, tmp_path: Path) -> Path:
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.28.0\nflask>=2.0\n")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myapp"\n'
            'dependencies = ["pydantic==2.5.0", "click"]\n'
        )
        return tmp_path

    def test_scan_identifies_upgradable(self, tmp_path: Path):
        repo = self._setup_repo(tmp_path)
        pypi = MagicMock(spec=PyPIClient)
        pypi.latest_stable_version.side_effect = lambda name: {
            "requests": "2.31.0",
            "flask": "3.0.0",
            "pydantic": "2.8.0",
            "click": "8.1.7",
        }.get(name)

        result = scan_upgradable_dependencies(repo, pypi)

        assert isinstance(result, UpgradableScanResult)
        assert result.total_declarations == 4
        items_by_name = {i.package: i for i in result.items}

        # requests: pinned 2.28.0 → 2.31.0 = upgradable
        assert items_by_name["requests"].status == "upgradable"
        assert items_by_name["requests"].current_version == "2.28.0"
        assert items_by_name["requests"].registry_latest == "2.31.0"
        assert items_by_name["requests"].cross_major is False

        # flask: range spec, no exact pin → unresolved
        assert items_by_name["flask"].status == "unresolved"

        # pydantic: pinned 2.5.0 → 2.8.0 = upgradable
        assert items_by_name["pydantic"].status == "upgradable"
        assert items_by_name["pydantic"].cross_major is False

        # click: no specifier → unresolved
        assert items_by_name["click"].status == "unresolved"

    def test_scan_up_to_date(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.31.0\n")
        pypi = MagicMock(spec=PyPIClient)
        pypi.latest_stable_version.return_value = "2.31.0"

        result = scan_upgradable_dependencies(tmp_path, pypi)
        assert result.items[0].status == "up_to_date"

    def test_scan_cross_major(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text("pydantic==1.10.0\n")
        pypi = MagicMock(spec=PyPIClient)
        pypi.latest_stable_version.return_value = "2.8.0"

        result = scan_upgradable_dependencies(tmp_path, pypi)
        assert result.items[0].status == "upgradable"
        assert result.items[0].cross_major is True

    def test_scan_lookup_failed(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text("obscure-pkg==1.0.0\n")
        pypi = MagicMock(spec=PyPIClient)
        pypi.latest_stable_version.side_effect = Exception("network error")

        result = scan_upgradable_dependencies(tmp_path, pypi)
        assert result.items[0].status == "lookup_failed"

    def test_scan_no_manifests(self, tmp_path: Path):
        pypi = MagicMock(spec=PyPIClient)
        result = scan_upgradable_dependencies(tmp_path, pypi)
        assert result.total_declarations == 0
        assert len(result.items) == 0


# ---------------------------------------------------------------------------
# D.4: Router scan_upgradable intent
# ---------------------------------------------------------------------------


class TestRouterScanIntent:
    """Router identifies scan_upgradable intent."""

    def test_scan_keyword_with_repo(self):
        router = Router()
        intent = router.route(
            "https://github.com/user/repo 扫描所有依赖"
        )
        assert intent.kind == "scan_upgradable"

    def test_scan_keyword_en_with_repo(self):
        router = Router()
        intent = router.route(
            "https://github.com/user/repo scan dependencies"
        )
        assert intent.kind == "scan_upgradable"

    def test_scan_keyword_without_repo_not_scan(self):
        """Without a repo URL, scan keyword alone is not enough."""
        router = Router()
        intent = router.route("扫描所有依赖")
        # No repo → cannot be scan_upgradable
        assert intent.kind != "scan_upgradable"
