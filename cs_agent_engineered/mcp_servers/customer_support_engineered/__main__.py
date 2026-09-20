"""MCP server: the customer-support backend, redesigned for agents (engineered).

The "good" side of the §2a contrast. Same backend, same data, same
capabilities as the first-cut agent's in-process `tools.py` — but every tool
was rewritten with
the agent in mind: action-verb names, typed parameters, structured returns,
explicit error contracts.

State flows in via:

  - Env vars at launch (refund cap, agent identity)
  - Explicit tool parameters per call (customer_id, order_id, etc.)
  - The mock backend (CustomerSupportClient reads/writes mocks/data/*.json)

There's no session closure here (we're a separate Python interpreter from
the agent). That's exactly how a production team's MCP server works:
they don't know or care about your agent's session — you pass everything
they need on each call.

Every order tool takes `customer_id` as its first parameter and verifies
that the order belongs to that customer before reading or writing. The
agent gets the verified customer_id from the message prefix; the tool
enforces ownership server-side so a wrong (or injected) `order_id` can't
leak or mutate someone else's order. Returns
`{"error": "ownership_mismatch", "code": 403, ...}` on mismatch.

Run via stdio (Strands' MCPClient launches it as a subprocess from
agent/core.py).
"""

import logging
import sys
from pathlib import Path

# Shared mocks/ package lives at the lab root, three levels up from this file.
_LAB_ROOT = Path(__file__).parent.parent.parent.parent
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from mcp.server.fastmcp import FastMCP

from agent.identity import AgentIdentity
from agent.profile import load_profile
from mocks.client import CustomerSupportClient

# Silence the MCP framework's INFO logs (otherwise every list_tools call
# leaks into the on-stage trace).
logging.basicConfig(level=logging.WARNING)
for noisy in ("mcp", "mcp.server", "mcp.server.lowlevel", "FastMCP"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

mcp = FastMCP("customer-support-engineered")

# State at launch. The server reads the same agent-profile.yaml as the
# agent — single source of truth for agent_id / name / refund_cap. In a
# real deployment this would come from the team's own config service or
# service-principal credentials.
_profile = load_profile()
_identity = AgentIdentity(
    agent_id=_profile.agent_id,
    name=_profile.name,
    refund_cap_usd=_profile.refund_cap_usd,
)
# The mock backend's data files are scoped to this agent_id so engineered's writes
# never leak into first-cut's view of the world (and vice versa).
_client = CustomerSupportClient(agent_id=_profile.agent_id)


def _load_owned_order(customer_id: str, order_id: str):
    """Fetch an order and verify it belongs to `customer_id`.

    Returns (order, None) on success, or (None, error_dict) if the order
    doesn't exist or belongs to someone else. The ownership check is
    enforced here so callers can't forget it — broken object-level
    authorization (OWASP API #1) is one tool signature away if you make
    `customer_id` optional or trust the agent to check.
    """
    o = _client.get_order(order_id)
    if o is None:
        return None, {"error": "order_not_found", "order_id": order_id}
    if o.customer_id != customer_id:
        return None, {
            "error": "ownership_mismatch",
            "code": 403,
            "order_id": order_id,
            "detail": "this order does not belong to the verified customer",
            "remediation": "do_not_act",
        }
    return o, None


# ----- Read tools -----------------------------------------------------


@mcp.tool()
def lookup_customer(customer_id: str) -> dict:
    """Look up a customer's profile by their customer_id.

    Call this first when you need to verify who you're speaking with,
    or when you need their tier, verification status, or email.
    Returns the customer record, or {"error": "customer_not_found"}.
    """
    c = _client.get_customer(customer_id)
    if c is None:
        # Don't echo customer_id back — keeps the trusted session ID out of
        # the conversation transcript even on the error path, so prompt
        # injection can't read it out of a not-found response.
        return {"error": "customer_not_found"}
    # Strip customer_id from the response. The harness binds it on every
    # call (see CustomerIdBindingHook); the LLM never needs a syntactic
    # handle on it and shouldn't get one from a tool result either.
    return c.model_dump(exclude={"customer_id"})


@mcp.tool()
def get_order(customer_id: str, order_id: str) -> dict:
    """Fetch a single order by order_id, scoped to the verified customer.

    `customer_id` is the verified ID from the message prefix — pass it
    directly. The server checks the order belongs to that customer; a
    mismatch returns {"error": "ownership_mismatch", "code": 403, ...}
    and you should NOT retry with a different customer_id.

    On success returns: status, items, total_usd, shipping_address,
    delivery_days_late, damaged flag.
    """
    o, err = _load_owned_order(customer_id, order_id)
    if err is not None:
        return err
    return o.model_dump(exclude={"customer_id"})


@mcp.tool()
def get_customer_orders(customer_id: str, limit: int = 5) -> dict:
    """List a customer's recent orders, most recent first, capped at `limit`.

    The result reports `total` and `truncated`; if `truncated` is true, raise
    `limit` before concluding an order does not exist.

    Use when the customer doesn't give an order number, or when you need
    their history to make a judgment call (e.g., repeat damaged delivery
    signals a warehouse problem, not just a customer one).
    """
    items = _client.get_customer_orders(customer_id)
    items.sort(key=lambda o: o.placed, reverse=True)
    shown = items[:limit]
    # Report the truncation instead of hiding it. A list that looks complete
    # but silently drops the order the customer is asking about sends the
    # model looking for the closest match among the rows it can see.
    return {
        "orders": [o.model_dump(exclude={"customer_id"}) for o in shown],
        "returned": len(shown),
        "total": len(items),
        "truncated": len(items) > len(shown),
    }


@mcp.tool()
def get_open_tickets(customer_id: str) -> dict:
    """Return tickets opened for this customer that are still status=open.

    Call this whenever episodic memory points at a ticket (e.g. "verify
    TICKET-1001 progress before replying") OR before escalating something
    that may already be tracked. Each result includes ticket_id, priority,
    reason, opened_at — enough to decide whether the original issue is
    still hanging vs. resolved.

    Empty list = no open tickets on file. If a ticket your memory referenced
    is NOT in here, treat memory as stale and update it via `append_memory`.
    """
    tickets = _client.get_open_tickets(customer_id)
    return {"tickets": tickets, "count": len(tickets)}


@mcp.tool()
def get_refund_history(customer_id: str) -> dict:
    """Return every refund previously issued to this customer.

    Call this BEFORE any new refund — episodic memory may claim "$X already
    refunded on order Y", but this ledger is the source of truth.

    Each entry includes:
      - `ref`              — refund reference id
      - `order_id`         — the order the refund was issued against
      - `amount_usd`       — dollar amount logged
      - `refund_percentage`— the fraction of order.total_usd this refund
        represented (e.g. 0.10 for a 10% shipping credit). SUM these per
        order to know how much percentage you have left to refund before
        you hit the category cap from `refund_calculation`. No dollar-math
        needed.
      - `reason`, `issued_at`, `agent_id` — context.

    Use it to:
      - avoid double-refunding an order (anti-split policy)
      - subtract prior `refund_percentage` from the category percentage
        when issuing a new refund on the same order
      - cite a specific past `ref` in your reply when relevant
    """
    refunds = _client.get_refund_history(customer_id)
    return {"refunds": refunds, "count": len(refunds)}


# ----- Write tools ---------------------------------------------------


@mcp.tool()
def update_shipping_address(customer_id: str, order_id: str, new_address: str) -> dict:
    """Update the shipping address on an order that has NOT yet shipped.

    Scoped to the verified customer — pass `customer_id` from the
    message prefix. Ownership is enforced server-side; a mismatch
    returns {"error": "ownership_mismatch", "code": 403, ...}.

    Returns {"error": "order_already_shipped", "remediation": "redirect_to_carrier"}
    if status is other than 'placed' or 'preparing'. On success:
    {"ok": True, "ref": "<audit_ref>"}.
    """
    o, err = _load_owned_order(customer_id, order_id)
    if err is not None:
        return err
    if o.status not in ("placed", "preparing"):
        return {
            "error": "order_already_shipped",
            "order_id": order_id,
            "current_status": o.status,
            "remediation": "redirect_to_carrier",
        }
    ref = _client.update_shipping_address(order_id, new_address, _identity.agent_id)
    return {"ok": True, "ref": ref}


@mcp.tool()
def cancel_order(customer_id: str, order_id: str, reason: str) -> dict:
    """Cancel an order that has NOT yet shipped.

    Scoped to the verified customer. `reason` is logged to the audit
    ledger. Same ownership and already-shipped error contracts as
    `update_shipping_address`.

    IMPORTANT — cancel_order does NOT issue a refund. It only flips the
    order's status to `cancelled` and writes a `cancel` ledger entry.
    If the customer paid for this order, you MUST call `issue_refund`
    afterwards or the customer never gets their money back.

    Post-condition (mandatory unless the order was never charged):

      1. After this returns `{"ok": True, ...}`, look up the
         `refund_calculation` policy for the current cancellation percentage.
      2. Call `get_refund_history(customer_id)` and SUM the prior
         `refund_percentage` values for THIS `order_id`. That's
         `already_refunded_pct`.
      3. Compute `net_pct = cancellation_pct - already_refunded_pct`.
      4. If `net_pct > 0`, call
         `issue_refund(order_id, refund_percentage=net_pct,
                       reason="cancel_net_of_prior")`.
         If `net_pct <= 0`, do NOT call issue_refund — the customer
         has already been refunded the full cancellation entitlement
         from a prior credit. Explain that in the reply.
         `issue_refund` itself enforces the refund-authority cap; if
         it returns a `policy_violation` 403, escalate the full refund
         amount as a single ticket (do NOT split).
      5. Confirm BOTH refs (cancel + refund) in the reply, or explain
         why no refund was issued.

    Skipping the refund step is the most common cancel-flow bug. The
    audit ledger will show `cancel` with no following `refund` entry
    on the same `order_id` and the customer will open another ticket.

    See the `handle-cancellation` skill for the full procedure.
    """
    o, err = _load_owned_order(customer_id, order_id)
    if err is not None:
        return err
    if o.status not in ("placed", "preparing"):
        return {
            "error": "order_already_shipped",
            "order_id": order_id,
            "current_status": o.status,
            "remediation": "contact_customer_for_return",
        }
    ref = _client.cancel_order(order_id, reason, _identity.agent_id)
    return {
        "ok": True,
        "ref": ref,
        "next_step": (
            "issue_refund is NOT automatic. If the customer was charged, "
            "compute the net refund (cancellation_pct minus any prior "
            "refund_percentage on this order from get_refund_history) and "
            "call issue_refund. See handle-cancellation skill."
        ),
    }


@mcp.tool()
def issue_refund(
    customer_id: str, order_id: str, refund_percentage: float, reason: str
) -> dict:
    """Issue a refund as a percentage of the order's total_usd.

    `refund_percentage` is a fraction in (0, 1] — e.g. `0.25` for 25% or
    `1.0` for a full refund. The server multiplies it by the order's
    `total_usd` to get the dollar amount logged in the ledger and checked
    against the agent's cap. The agent does NOT pass the dollar amount;
    pick the right percentage instead.

    Procedure the agent MUST follow (otherwise audit will flag the call):

      1. Call `search_policy_kb` for `refund_authority` FIRST. It states
         your dollar cap, the anti-split rule, and that over-cap refunds
         must be escalated as a single ticket (not retried smaller).
         Knowing the cap up front lets you short-circuit obvious over-cap
         requests straight to escalation without spending tool calls on
         category / history math you won't use.
      2. Call `search_policy_kb` for `refund_calculation` to get the
         percentage for the refund category (damaged / cancellation /
         shipping_delay / return_window). Do NOT pick a number from memory.
      3. Call `get_refund_history(customer_id)`, filter entries by
         THIS `order_id`, and SUM their `refund_percentage` values.
         That sum is `already_refunded_pct`. The ledger records the
         fraction every prior refund used — no amount/total math.
      4. Compute net: `net_pct = category_pct - already_refunded_pct`.
      5. If `net_pct <= 0`, do NOT call this tool — escalate instead; the
         customer has already been refunded everything policy allows.
      6. Re-check against the cap with the concrete amount. If
         `net_pct * order.total_usd` exceeds the cap from step 1, escalate
         the FULL amount as a single ticket. Do NOT split.
      7. Pass `net_pct` as `refund_percentage`. The server logs both pct
         and dollar amount.

    Ordering constraint for cancellation refunds: if this refund is the
    money-back leg of a cancellation, `cancel_order` MUST have already
    succeeded on this order BEFORE you call this tool. Refunding first
    and then cancelling leaves a window where you've paid out on a
    still-active order; if the cancel later rejects (e.g. it shipped
    between calls), you have to reverse the refund. Always cancel first.

    Errors:
      - {"error": "ownership_mismatch", "code": 403, ...} — wrong customer,
        do NOT retry with a different `customer_id`.
      - {"error": "invalid_pct", ...} — `refund_percentage` not in (0, 1].
      - {"error": "policy_violation", "code": 403,
         "detail": "refund of $X exceeds agent cap of $Y",
         "remediation": "escalate_to_human"} — over cap. PERMANENT;
        do NOT split into smaller calls — that violates `refund_authority`.

    Use specific `reason` values:
      - "shipping_delay_credit" for delay compensation
      - "damaged_item_full_refund" / "damaged_item_partial" for damage
      - "return_within_window" for normal returns
      - "cancellation_refund" / "cancel_net_of_prior" for cancellations
    """
    o, err = _load_owned_order(customer_id, order_id)
    if err is not None:
        return err

    if not isinstance(refund_percentage, (int, float)) or refund_percentage <= 0 or refund_percentage > 1:
        return {
            "error": "invalid_pct",
            "detail": (
                f"refund_percentage must be a fraction in (0, 1] (e.g. 0.10 for 10%), "
                f"got {refund_percentage!r}"
            ),
        }

    amount_usd = round(float(refund_percentage) * float(o.total_usd), 2)

    allowed, why = _identity.can_refund(amount_usd)
    if not allowed:
        return {
            "error": "policy_violation",
            "code": 403,
            "detail": why,
            "remediation": "escalate_to_human",
        }

    ref = _client.issue_refund(
        order_id,
        o.customer_id,
        amount_usd,
        reason,
        _identity.agent_id,
        refund_percentage=float(refund_percentage),
    )
    return {
        "ok": True,
        "ref": ref,
        "refund_percentage": float(refund_percentage),
        "amount_usd": amount_usd,
    }


# ----- Escalation --------------------------------------------------


@mcp.tool()
def escalate_to_human(
    reason: str,
    priority: str = "normal",
    customer_id: str | None = None,
) -> dict:
    """Open a ticket for a human agent to handle.

    Use when:
      - the action exceeds your authority (e.g., refund > cap)
      - the situation is ambiguous and needs human judgment
      - the customer explicitly asks for a human
      - input guardrails have flagged the message as adversarial

    `reason` is **the customer's reason** for the escalation — what they
    want and why, on their behalf. One or two sentences in plain English.
    Do NOT include your reasoning chain, the tool calls you made, audit
    pointers, customer profile fields, or what you'd like the human to
    do — the human will pull all of that from the order/ticket/refund
    APIs themselves. Keep it about the customer.

      ✅ GOOD: "Customer wants a refund to the original card instead of
                the $10 store credit already issued — says the 4-day
                delay made the item useless to them."

      ❌ BAD:  "Customer: Alice Chen, standard tier. Orders touched:
                #1234. Audit: refund_0002 store credit $10 issued
                2026-05-16 by cs-agent-engineered. What I did: get_order,
                get_refund_history, search_policy_kb (shipping_delay).
                Requested action: human review to approve refund to card
                OR offer replacement..."

    `priority` must be one of: 'low', 'normal', 'high', 'urgent'.
    """
    if priority not in {"low", "normal", "high", "urgent"}:
        return {
            "error": "invalid_priority",
            "allowed": ["low", "normal", "high", "urgent"],
        }
    ticket_id = _client.open_ticket(reason, priority, _identity.agent_id, customer_id)
    return {"ok": True, "ticket_id": ticket_id, "priority": priority}


if __name__ == "__main__":
    mcp.run(transport="stdio")
