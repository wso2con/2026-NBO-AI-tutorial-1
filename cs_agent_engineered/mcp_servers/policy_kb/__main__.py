"""Local MCP server exposing the company policy KB.

Runs as a subprocess of the agent (launched via Strands' MCPClient from
agent/core.py). It exposes the SAME corpus as first-cut's `search_policy_kb`,
but as two deliberate steps instead of one fuzzy guess:

    list_policies()          -> the whole catalogue, ids + titles
    get_policy(policy_id)    -> one policy in full

first-cut asks the model to invent a query string and returns whatever scores
highest, so the model never learns what else was in the KB and a badly-phrased
query silently returns the wrong rule. Listing first makes the selection an
explicit, auditable decision in the trace: you can see which policies the
model knew about and which it chose to read.

Same corpus, same content, different ergonomics — that is the whole lesson.
Telling the LLM to consult policy isn't the same as wiring it to.

In production this would be a hosted service owned by the policy /
compliance team. The agent team never edits policy content — they just
call the MCP tool.

To run standalone (for debugging):
    python -m mcp_servers.policy_kb
"""

import logging
import re
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

# This server owns its own view of the corpus. `policies/` is shared with the
# first-cut agent, whose `search_policy_kb` keeps using `policies.search`
# unchanged — nothing here alters what that agent sees.
_POLICIES_DIR = _LAB_ROOT / "policies"


def _parse_policy(path: Path) -> dict:
    """Parse one policy file into `{id, title, keywords, rule}`.

    Format is YAML-ish frontmatter plus a markdown body. A file without
    frontmatter still yields a usable record keyed on its filename.
    """
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return {
            "id": path.stem,
            "title": path.stem.replace("_", " ").title(),
            "keywords": [],
            "rule": text.strip(),
        }

    front_matter, body = match.groups()
    fields: dict = {}
    for line in front_matter.splitlines():
        key, _, value = line.partition(":")
        if not _:
            continue
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        fields[key] = value

    keywords = fields.get("keywords", [])
    return {
        "id": fields.get("id", path.stem),
        "title": fields.get("title", path.stem),
        "keywords": keywords if isinstance(keywords, list) else [],
        "rule": body.strip(),
    }


def _load_policies() -> list[dict]:
    """Every policy on disk, id-sorted. Empty list if the directory is gone."""
    if not _POLICIES_DIR.exists():
        return []
    return sorted(
        (_parse_policy(path) for path in _POLICIES_DIR.glob("*.md")),
        key=lambda policy: policy["id"],
    )

mcp = FastMCP("policy-kb")


@mcp.tool()
def list_policies() -> dict:
    """List every policy in the knowledge base.

    Returns `{"policies": [{id, title}], "count": n}` - the catalogue only,
    no policy bodies. Each title says what that policy covers, so read the
    list, decide which policies your case actually needs, then fetch each one
    with `get_policy(policy_id)`.

    Choosing deliberately from a complete list beats guessing a search
    query: the catalogue tells you what exists, including the policies you
    did not think to look for.

    Call this when the request may turn on a policy - money, entitlement, or
    a change to an order. A purely informational answer (order status,
    account details) needs no policy at all; don't spend a call on it.
    """
    policies = [{"id": p["id"], "title": p["title"]} for p in _load_policies()]
    return {"policies": policies, "count": len(policies)}


@mcp.tool()
def get_policy(policy_id: str) -> dict:
    """Fetch one policy in full by its `policy_id` from `list_policies()`.

    Returns `{id, title, rule}` — `rule` is the authoritative body. Call it
    once per policy you need; a case often needs more than one (e.g. the
    category policy for evidence rules, plus `refund_calculation` for the
    percentage).

    Always read the relevant policies BEFORE taking a compensating action
    (refund, credit, cancellation, address change). Don't work from a
    remembered rule — they change, and the audit trail proves you checked.

    An unknown `policy_id` returns a structured `unknown_policy` error
    listing the valid ids; re-read the catalogue rather than guessing again.
    """
    policies = _load_policies()
    for policy in policies:
        if policy["id"] == policy_id:
            return {
                "id": policy["id"],
                "title": policy["title"],
                "rule": policy["rule"],
            }
    return {
        "error": "unknown_policy",
        "code": 404,
        "detail": f"no policy with id={policy_id!r}",
        "valid_ids": [p["id"] for p in policies],
        "remediation": "call list_policies and choose an id from it",
    }


if __name__ == "__main__":
    # Stdio transport — what the agent connects to via Strands' MCPClient
    mcp.run(transport="stdio")
