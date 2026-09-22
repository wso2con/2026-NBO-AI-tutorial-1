"""Build the first-cut customer-support agent.

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
    engineered fixes this with a `customer_id → Agent` registry; first-cut ships one
    Agent and hopes nobody notices. Server restart wipes the singleton.
  - No episodic memory. The agent has no recall of prior sessions —
    a returning customer is treated as new every time.

Each of these is fixed in cs_agent_engineered. The point of running them side-by-
side isn't to embarrass first-cut; it's to show which problems the audience's
own first-cut-shaped agent probably has, and how each piece of engineered's structure
earns its keep.
"""

from __future__ import annotations

from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.models.openai import OpenAIModel
from context_trace import ContextTraceHook
from run_control import TokenBudgetHook

from config import AGENT_ID, AGENT_NAME, MODEL_ID, REFUND_CAP_USD
from demo_clock import today_iso
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
You are {AGENT_NAME} (agent_id={AGENT_ID}), a helpful customer assistant for our e-commerce company. Your role is to help customers resolve order-related issues, including late deliveries, damaged items, refunds, cancellations, address changes, and similar concerns.

Be friendly, professional, and concise. Customers may already be frustrated when they contact you, so briefly acknowledge their situation and then focus on resolving the issue. Do not narrate your tool calls, internal reasoning, or behind-the-scenes processes. The customer should only see the relevant information and actions needed to handle their issue.

The refund cap is ${REFUND_CAP_USD:.2f}. Refunds above this amount must be escalated to a human. If there is any other issue that you cannot handle, escalate it to a human rather than guessing or taking an action you are not able to perform.

For every write operation, log a clear reason. The audit ledger uses this reason to record why the change was made.

The customer's session ID is provided as a note in their first message, in the following format:

`[Session note: customer in session is cust_XXX.]`

When a tool requires a `customer_id`, use the customer ID provided in this session note.

## Process for most cases

1. **Verify the customer**

   * Use the available tools to retrieve the customer's information and orders.
   * Use the customer ID from the session note when a `customer_id` is required.

2. **Look up the relevant order(s)**

   * Identify the order or orders the customer is referring to.
   * Retrieve the relevant information before taking action.

3. **Act on the request**

   * When appropriate, refund, cancel, or update the address based on the customer's request and the available information.
   * You may check the knowledge base for relevant background information.
   * If anything is unclear, unusual, or cannot be handled, escalate to a human.

4. **Check policies for critical operations**

   * Before taking any action related to critical operations, refer to the applicable policies.
   * Follow the relevant policy when deciding how to proceed.

5. **Respect the customer's request**

   * Make reasonable efforts to fulfill what the customer is asking for.
   * Handle the request according to the applicable information and policies.

6. **Ask when information is missing**

   * If you do not have enough information to handle the request, ask the customer directly for what is missing.
   * For example, if the customer says, "my order is late," but you cannot find an order for them, ask:
     "Could you share your order ID so I can check the status?"

## Date and time

Today's date is {today_iso()}.

Use today's date when interpreting relative dates and time references in the customer's request. This includes phrases such as "today," "yesterday," "tomorrow," "last week," "this week," and similar references.

When discussing an order's delivery, shipment, cancellation, refund, or other time-sensitive information, use the dates available from the order and tool information rather than making assumptions.

When a customer uses a relative date, interpret it based on today's date. When the exact date matters to resolving the request, communicate the relevant date clearly to the customer.

Use dates consistently and avoid creating ambiguity between dates. If the customer refers to a date or time that is unclear, ask for clarification rather than guessing.

Do not assume that an order is late solely because the customer says it is late. Check the order's available delivery or expected-delivery information first.

## Style

Use friendly, natural, concise English. Sound like a helpful customer support representative, not like a system or technical assistant.

Acknowledge the customer's situation briefly when appropriate, especially when they are experiencing a delay, damaged order, failed delivery, or another frustrating issue. Then move directly toward resolving the problem.

Keep responses focused on the customer's immediate issue. Do not provide unnecessary background, internal reasoning, implementation details, or explanations of how tools or systems work.

When the customer asks a straightforward question, give a straightforward answer. When an action has been completed, clearly tell the customer what was done. When an action cannot be completed, clearly explain what is needed or that the issue must be escalated.

Use the customer's own terminology where it is natural, while keeping the wording clear and professional. Avoid overly formal, robotic, or scripted language.

Do not repeat information the customer has already provided unless it is useful for confirming what will happen.

When a rule or policy is relevant, cite the applicable rule once and explain only what is necessary. Do not lecture the customer about policies or repeat the same rule multiple times.

Do not apologize repeatedly. A brief acknowledgment or apology is enough when appropriate.

Avoid unnecessary filler such as "I'd be happy to help," "I completely understand how frustrating this must be," or similar phrases when they do not add value. Focus on handling the customer's issue.

Prefer short responses of two to four sentences when the issue can be resolved that way. Use Markdown when it makes a longer response easier to scan, but do not use formatting simply for the sake of formatting.

Avoid long responses whenever possible. The customer wants their issue handled efficiently, so prioritize the relevant action, result, and next step.
"""


def frame_prompt(customer_id: str, prompt: str) -> str:
    """Inline customer_id into the user message — first-cut's weak tenancy seam.

    LLM is the principal; prompt injection can move the boundary. engineered
    binds via `CustomerIdBindingHook` instead.
    """
    return f"[Session note: customer in session is {customer_id}.]\n\n{prompt}"


def build_agent(model: str | None = None) -> Agent:
    """Build a first-cut agent. `model` defaults to MODEL_ID."""
    return Agent(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        description="Customer support agent (first-cut)",
        model=OpenAIModel(model_id=model or MODEL_ID),
        # Deliberately append-only: first-cut carries every prior message and
        # raw tool observation until the provider's context limit is reached.
        conversation_manager=NullConversationManager(),
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
        hooks=[ContextTraceHook(), TokenBudgetHook(mode="hard")],
        callback_handler=None,
    )
