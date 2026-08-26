"""The read-only MCP server.

Importing this package does not start a server. Use
``campaign_preflight.mcp.server.build_server`` or the ``campaign-preflight-mcp``
console script.
"""

from .formatting import report_id, summarize_report

__all__ = ["report_id", "summarize_report"]
