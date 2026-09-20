"""Strands hooks for the customer-support agent.

`CustomerIdBindingHook` — the harness-side user identity binding.

OWASP API #1 is "Broken Object Level Authorization." The naive fix in the rest
of this lab is to (a) inject `[verified customer_id=X]` into the message and
(b) check ownership server-side on every tool. That's defense in depth, but
the LLM is still the principal — a prompt injection can make the model pass
the wrong `customer_id` to any tool whose docstring it can reach. Server-side
ownership checks like `_load_owned_order` catch order-scoped abuse, but
customer-scoped reads (e.g. `get_refund_history(customer_id)`) have nothing
to check against once the LLM picks the ID.

This hook moves the principal one layer up. The harness — not the model —
owns `customer_id` on every customer-scoped tool call. If the LLM proposes
a `customer_id` that doesn't match the session's trusted ID, the call is
rejected with a 403 *before* it ships to the MCP server. If the LLM omits
the argument entirely (typical of an early discovery call), the harness
fills in the trusted value. Either way, the model has no syntactic handle
on the identifier that actually scopes data access.

Why reject rather than silently rewrite: rewriting hides the attack and
also creates an agent-loop hazard — the model asks for `cust_003`, sees
data for `cust_001` come back, decides its call "didn't work," and retries
forever. An explicit 403 breaks the loop and makes the cross-tenant
attempt visible in the trace.

The pattern is exactly the same as a backend rejecting a request whose
body claims a different user than the Authorization header: the LLM
proposes the call shape, the harness verifies it.
"""

import json
from dataclasses import dataclass

from strands.hooks import HookRegistry
from strands.hooks.events import BeforeToolCallEvent

# Tools whose input dicts get `customer_id` injected. Anything NOT in this set
# is left alone — e.g., `search_policy_kb` (no customer scoping) or the
# AgentSkills `skills` loader. Keep the list explicit so it's clear which
# tools depend on user identity binding.
CUSTOMER_SCOPED_TOOLS = frozenset(
    {
        "lookup_customer",
        "get_order",
        "get_customer_orders",
        "get_open_tickets",
        "get_refund_history",
        "update_shipping_address",
        "cancel_order",
        "issue_refund",
        "escalate_to_human",
        "append_memory",
        "compact_memory",
    }
)


@dataclass
class CustomerIdBindingHook:
    """Validate `customer_id` on every customer-scoped tool call against the trusted session ID.

    See the module docstring for the OWASP API#1 rationale.
    """

    customer_id: str

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._bind_customer_id)

    def _bind_customer_id(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        name = tool_use.get("name")
        if name not in CUSTOMER_SCOPED_TOOLS:
            return
        inputs = tool_use["input"]
        proposed = inputs.get("customer_id")
        if proposed is None or proposed == "":
            # Early discovery call — model hasn't seen the ID yet. Fill in
            # the trusted value so first-touch tools (lookup_customer, etc.)
            # work without the model having to know the ID.
            inputs["customer_id"] = self.customer_id
            return
        if proposed == self.customer_id:
            return
        # Mismatch: the model proposed a customer_id that isn't the session's.
        # Strong signal of prompt injection or cross-tenant probing. Reject
        # with a 403 so the trace shows the attempt and the model gets a
        # clear "stop" rather than confusingly-rewritten data. We intentionally
        # do NOT echo the trusted ID back — that would just feed the attacker
        # the value they were trying to discover.
        event.cancel_tool = json.dumps(
            {
                "error": "unauthorized",
                "code": 403,
                "detail": (
                    f"user is not authorized to access customer_id={proposed!r}"
                ),
                "remediation": "reject the user's request, and request to login with correct account.",
            }
        )


@dataclass
class RefundCapHook:
    """Enforce the agent's per-call refund cap at the harness — defense-in-depth above the MCP server's own check.

    `issue_refund` now takes a `refund_percentage` (a fraction in (0, 1]) and the
    server multiplies by the order's `total_usd` to get the dollar amount.
    The hook needs its own `CustomerSupportClient` instance to look up the
    order's total before it can decide whether the resulting amount blows
    the cap. The harness and the v2 MCP subprocess each carry their own
    client; both read the same per-agent disk dir (`mocks/data/<agent_id>/`)
    so they see the same world.
    """

    refund_cap_usd: float
    agent_id: str

    def __post_init__(self) -> None:
        from mocks.client import CustomerSupportClient
        self._client = CustomerSupportClient(agent_id=self.agent_id)

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._enforce_cap)

    def _enforce_cap(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        if tool_use.get("name") != "issue_refund":
            return
        inputs = tool_use.get("input") or {}
        pct = inputs.get("refund_percentage")
        order_id = inputs.get("order_id")
        if not isinstance(pct, (int, float)) or pct <= 0 or pct > 1:
            return  # let the server return the structured invalid_pct error
        # Refresh the per-instance cache so we see any writes made by the MCP
        # subprocess this session (issue_refund / cancel_order). Without this,
        # the cap would be computed against stale totals after a status flip.
        self._client._reload_cache()
        order = self._client.get_order(order_id) if order_id else None
        if order is None:
            return  # let the server return ownership_mismatch / not_found
        amount = round(float(pct) * float(order.total_usd), 2)
        if amount <= self.refund_cap_usd:
            return
        # cancel_tool accepts a string that becomes an error tool-result body.
        # We hand back JSON matching the v2 server's 403 shape so run.py's
        # trace renderer recognises it and shows a clean red error.
        event.cancel_tool = json.dumps(
            {
                "error": "policy_violation",
                "code": 403,
                "detail": (
                    f"refund of ${amount:.2f} (={pct:.2%} of ${order.total_usd:.2f}) "
                    f"exceeds agent cap of ${self.refund_cap_usd:.2f}"
                ),
                "remediation": "escalate_to_human",
            }
        )
