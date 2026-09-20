"""Runtime configuration for cs_agent_v1.

Environment variables with sensible defaults — the classic first-cut
configuration shape. Quick to ship, works, but scattered: the model
and cap are here, the prompt is in `agent.py`, the tool list is in
`tools.py`. No single source of truth for "what does this agent do?"

cs_agent_v2 collapses all of this into one declarative
`agent-profile.yaml` that the harness reads at startup
(see `cs_agent_v2/agent-profile.yaml`).
"""

from __future__ import annotations

import os

AGENT_ID = os.environ.get("AGENT_ID", "cs-agent-v1")
AGENT_NAME = os.environ.get("AGENT_NAME", "Customer Support Agent")
MODEL_ID = os.environ.get("AGENT_MODEL", "gpt-5.4-mini")
REFUND_CAP_USD = float(os.environ.get("REFUND_CAP_USD", "200"))
# Max messages kept in `agent.messages` by Strands' SlidingWindowConversationManager.
# Drop low (e.g. 4) to demo the agent forgetting older turns.
CONVERSATION_WINDOW = int(os.environ.get("CONVERSATION_WINDOW", "20"))
