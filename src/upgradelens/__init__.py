"""UpgradeLens — a general-purpose, evidence-driven software-engineering agent.

UpgradeLens turns a plain-language request into a structured engineering task and
runs one of five capabilities — dependency upgrade, PR review, issue repair,
breaking-change analysis and security review — through a single controlled
execution layer (Supervisor + Handoff). The recommended entry point for new code
is :class:`EngineeringAgent`::

    from upgradelens import EngineeringAgent

    agent = EngineeringAgent(mode="fake")
    result = agent.run("review the security of https://github.com/o/r")
    for finding in result.findings:
        print(finding.severity, finding.summary)

For dependency-upgrade-only callers, :class:`DependencyUpgradeAgent` remains the
focused front door. The traditional pipeline and agent-loop functions
(``run_pipeline``, ``run_agent``) stay available for callers needing finer control.

CLI entry points:

- ``upgradelens`` — the CLI (``scan-dependency``, ``assess``, ``agent``,
  ``eval-compare``, ``eval-ablate``, ``eval-replay``, …);
- ``upgradelens-mcp`` — the MCP server.
"""

from upgradelens.agent.api import AgentResult, DependencyUpgradeAgent
from upgradelens.agent.engineering_agent import EngineeringAgent, EngineeringResult

__all__ = [
    "__version__",
    "AgentResult",
    "DependencyUpgradeAgent",
    "EngineeringAgent",
    "EngineeringResult",
]

__version__ = "0.3.0"
