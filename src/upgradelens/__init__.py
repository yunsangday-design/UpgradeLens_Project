"""UpgradeLens — evidence-driven dependency upgrade pre-audit agent.

UpgradeLens analyses a repository for the impact of upgrading a Python
dependency: it scans the codebase for usages of the dependency's API, retrieves
relevant documentation, and produces a verified impact report with specific
breaking-change risks, evidence citations, and migration recommendations.

The recommended entry point for new code is :class:`DependencyUpgradeAgent`::

    from upgradelens import DependencyUpgradeAgent

    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run("upgrade pydantic in ./repo to 2.0")
    print(result.verified.conclusion)

The traditional pipeline and agent-loop functions (``run_pipeline``,
``run_agent``) remain available for callers that need finer control.

CLI entry points:

- ``upgradelens`` — the CLI (``scan-dependency``, ``assess``, ``agent``,
  ``eval-compare``, ``eval-ablate``, ``eval-replay``, …);
- ``upgradelens-mcp`` — the MCP server.
"""

from upgradelens.agent.api import AgentResult, DependencyUpgradeAgent

__all__ = ["__version__", "AgentResult", "DependencyUpgradeAgent"]

__version__ = "0.2.0"
