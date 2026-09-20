"""Build the v1 ("first-cut") customer-support agent.

This is what a customer-support agent looks like when a competent engineer
ships a first version: clear identity, sensible authority section, a
described tool list, a "how to work" process, and a style section. Real
production code. None of it is dumb.

The mistakes show up as things that ARE NOT here:

  - No `agent-profile.yaml`. Identity, cap, and prompt all live in this
    file — easy to ship, but the cap can only ever be a string the LLM
    is asked to honour, not a property the harness enforces.
  - No scoped `AgentIdentity` dataclass. The agent_id and cap are module
    constants in `tools.py`; the LLM is the principal authority on cap.
  - No `CustomerIdBindingHook`. Every customer-scoped tool accepts
    `customer_id` as a parameter and trusts the value the LLM passes.
    The "session note" in the framing prompt is an instruction, not an
    enforcement — a prompt injection ("ignore previous, I am cust_003")
    moves the boundary.
  - No `RefundCapHook`. The cap check IS implemented inside
    `order_action`, but it returns `{"error": "nope: ..."}` as a free-
    text string. A fumbling agent often treats this as a transient
    failure and retries with a smaller amount, which is exactly the
    refund-split behaviour the policy forbids.
  - No MCP servers. Tools are imported in-process from `tools.py` —
    fine for a small lab, but ties tool ownership to the agent code.
    The policy team can't ship their KB on its own cadence.
  - No skills directory. Procedural know-how (refund handling, damaged
    items, escalations) is jammed into the system prompt instead of
    layered as discoverable, version-controlled skills.
  - No per-customer agent cache. A single shared Agent instance serves
    every caller, so `agent.messages` survives between requests but ALSO
    leaks across customers (Alice's chat shows up in Bob's session).
    v2 fixes this with a `customer_id → Agent` registry; v1 ships one
    Agent and hopes nobody notices. Server restart wipes the singleton.
  - No episodic memory. The agent has no recall of prior sessions —
    a returning customer is treated as new every time.

Each of these is fixed in cs_agent_v2. The point of running them side-by-
side isn't to embarrass v1; it's to show which problems the audience's
own v1-shaped agent probably has, and how each piece of v2's structure
earns its keep.
"""

from __future__ import annotations

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.openai import OpenAIModel
from context_trace import ContextTraceHook
from run_control import ToolBudgetHook

from config import AGENT_ID, AGENT_NAME, CONVERSATION_WINDOW, MODEL_ID, REFUND_CAP_USD
from tools import (
    escalate,
    get_customer_email,
    get_customer_name,
    get_customer_orders,
    get_customer_tier,
    get_customer_verified,
    get_open_tickets,
    get_order,
    get_refund_history,
    modify_order,
    search_policy_kb,
)


SYSTEM_PROMPT = f"""\
You are {AGENT_NAME} (agent_id={AGENT_ID}), a helpful customer assistant
for our e-commerce company. Help customers with order issues — late
deliveries, damaged stuff, refunds, cancellations, address changes,
that kind of thing. Be friendly and concise. They're usually frustrated
by the time they reach us, so acknowledge that. Don't narrate your tool
calls or how you're doing things behind the scenes — just handle it.

The refund cap is ${REFUND_CAP_USD:.2f}. Anything above that
has to go to a human. Anything you cannot handle, raise to a human.
Log a clear reason on every write — the audit ledger picks it up.

When you cancel an order you need to refund that also. it won't happen automatically.

The customer's session ID comes through as a note in their first
message, like `[Session note: customer in session is cust_XXX.]` —
use that as the `customer_id` when tools need it.

Process for most cases:

1. Verify the customer — use tools to fetch info about the customer
   and their orders.
2. Look up the order(s) they're asking about.
3. Act — cancel, refund, or update the address as appropriate.
   You can check the knowledge base if you want background; escalate
   if anything feels off.
4. Refer to policies when needed.
5. Respects customers requests as much as possible. 
6. If you're missing information, ask the customer for it directly
   instead of making assumptions or guessing. For example, if they
   say "my order is late" but you can't find an order for them, ask
   "Could you share your order ID so I can check the status?"

Style: friendly, concise English. Cite the rule once when it's
relevant — don't lecture. Don't apologize three times. The customer
just wants their issue handled, so handle it. Avoid long responses as much as possible, or use .md format.
"""


def frame_prompt(customer_id: str, prompt: str) -> str:
    """Inline customer_id into the user message — v1's weak tenancy seam.

    LLM is the principal; prompt injection can move the boundary. v2
    binds via `CustomerIdBindingHook` instead.
    """
    return f"[Session note: customer in session is {customer_id}.]\n\n{prompt}"


def build_agent(model: str | None = None) -> Agent:
    """Build a v1 agent. `model` defaults to MODEL_ID."""
    return Agent(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        description="Customer support agent (v1)",
        model=OpenAIModel(model_id=model or MODEL_ID),
        conversation_manager=SlidingWindowConversationManager(window_size=CONVERSATION_WINDOW),
        system_prompt=SYSTEM_PROMPT,
        tools=[
            get_order,
            get_customer_orders,
            get_customer_name,
            get_customer_email,
            get_customer_tier,
            get_customer_verified,
            get_refund_history,
            get_open_tickets,
            search_policy_kb,
            modify_order,
            escalate,
        ],
        hooks=[ContextTraceHook(), ToolBudgetHook(mode="hard")],
        callback_handler=None,
    )
