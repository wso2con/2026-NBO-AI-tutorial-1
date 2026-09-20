"""Local MCP server exposing the company policy KB.

Runs as a subprocess of the agent (launched via Strands' MCPClient from
agent/core.py). The agent calls `search_policy_kb(query)` like a native
tool; under the hood, this server delegates to `policies.search.search`
— the same retrieval first-cut's `search_kb` uses. Same corpus, same scoring,
same return shape.

The §6 Plan-before-commit lesson is intentionally NOT about retrieval
quality differences. Both agents have the same evidence available. What
differs is framing: this tool's docstring says "always call this BEFORE
compensating actions" and engineered has a `handle-refund` skill that names the
procedure; first-cut's wrapper has a one-line docstring and no skill. Same
KB; different ergonomics; different agent behaviour.

In production this would be a hosted service owned by the policy /
compliance team. The agent team never edits policy content — they just
call the MCP tool.

To run standalone (for debugging):
    python -m mcp_servers.policy_kb
"""

import logging
import sys
from pathlib import Path

# Silence MCP framework's INFO logs (e.g. "Processing request of type
# ListToolsRequest" on every agent build) — keeps the on-stage trace clean.
logging.basicConfig(level=logging.WARNING)
for noisy in ("mcp", "mcp.server", "mcp.server.lowlevel", "FastMCP"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Lab root on sys.path so we can import the shared `policies.search`
# module. This file is at cs_agent_engineered/mcp_servers/policy_kb/__main__.py
# — four parents up is the repo root.
_LAB_ROOT = Path(__file__).parent.parent.parent.parent
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402  — after logging config

from policies.search import search as _policy_search  # noqa: E402

mcp = FastMCP("policy-kb")


@mcp.tool()
def search_policy_kb(query: str) -> list[dict]:
    """Search the company policy knowledge base.

    Returns the most relevant policies for the query. Each result includes:
      - `id`           — short policy identifier (e.g., "damaged_item")
      - `title`        — human-readable title
      - `rule`         — the policy body (the authoritative content)
      - `relevance`    — keyword-match score

    Always call this BEFORE taking compensating actions (refunds, credits,
    address changes). Don't memorize rules — they change. The audit trail
    proves you checked.
    """
    return _policy_search(query)


if __name__ == "__main__":
    # Stdio transport — what the agent connects to via Strands' MCPClient
    mcp.run(transport="stdio")
