"""UpgradeLens MCP server package.

Exposes the analyzer as a set of MCP tools so any MCP-capable client
(Claude Desktop, an IDE extension, a CI bot) can drive the dependency
upgrade analysis without shelling out to the CLI.
"""

from upgradelens.mcp.server import mcp, serve

__all__ = ["mcp", "serve"]
