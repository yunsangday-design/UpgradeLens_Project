"""Agent front-end: turn natural-language requests into structured assessments."""

from upgradelens.agent.api import AgentResult, DependencyUpgradeAgent
from upgradelens.agent.router import Intent, Router, route

__all__ = ["AgentResult", "DependencyUpgradeAgent", "Intent", "Router", "route"]
