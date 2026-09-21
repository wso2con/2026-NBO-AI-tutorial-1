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
agent/core.py), or over HTTP for clients that are not this agent:

    python -m mcp_servers.customer_support_engineered --transport http

See `mcp_servers/_serve.py` for the transport options and what the HTTP
mode leaves open.
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
from mocks.client import CustomerSupportClient, consume_fault
from mcp_servers._serve import serve

# Silence the MCP framework's INFO logs (otherwise every list_tools call
# leaks into the on-stage trace).
logging.basicConfig(level=logging.WARNING)
for noisy in ("mcp", "mcp.server", "mcp.server.lowlevel", "FastMCP"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Server-level guidance, handed to every client at `initialize`. Tool
# docstrings say what one call does; this says how the calls fit together —
# the conventions and ordering rules that are invisible from any single
# signature, and that a consumer building an agent would otherwise have to
# rediscover by trial and error.
_INSTRUCTIONS = """Order-management backend for customer support: look up customers and orders,
then change shipping, cancel, refund, or escalate.

IDENTITY. Every customer-scoped tool takes `customer_id` first and verifies
server-side that the order belongs to that customer. Pass a real ID — the
demo dataset has `cust_001`, `cust_002` and `cust_003`. The sole exception is
this project's own agent harness, which binds the verified ID for you and
expects `""`; from any other client `""` returns `customer_not_found`.

READS. Start with `get_customer_orders`. One call returns enough per order —
status, total, dates, lateness, damage evidence — to both pick the order and
judge eligibility, so a follow-up `get_order` is usually a wasted round trip.
Bind the exact order the customer means before acting, and ask when their
reference is ambiguous rather than taking the order that happens to qualify.

WRITES. `cancel_order` does not refund; `issue_refund` is a separate call and,
for `reason_code="cancellation"`, is rejected until the cancel has succeeded.
`issue_refund` takes a fraction (0.25 = 25%), never a percentage, and its
`reason_code` is checked against the order's stored facts rather than trusted.

ERRORS are return values, not exceptions: a dict with `error` and usually a
`remediation` naming the next move. Read it before deciding. `service_timeout`
carries `retryable: false` — escalate, never retry.
"""

mcp = FastMCP("customer-support-engineered", instructions=_INSTRUCTIONS)

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
    """Look up a customer's profile: name, email, phone, tier, verification status.

    Use for tier or verification checks. Order questions do not need this
    first — go straight to `get_customer_orders`.

    Args:
        customer_id: The customer's ID, e.g. "cust_001". Pass "" only if the
            calling harness binds the verified ID for you.

    Returns:
        The profile, or `{"error": "customer_not_found"}`.
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
    """Fetch one order in full.

    Prefer `get_customer_orders`: its rows already carry status, items, total,
    dates, lateness and damage evidence. Reach for this only when you need a
    field those rows omit, such as the shipping address of an order that has
    already shipped.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).
        order_id: The order's ID, e.g. "1234". A leading "#" is stripped for you.

    Returns:
        The order record. `damage_photos` lists photo evidence already on
        file; an empty list means none has been filed. On failure:
        - `order_not_found` — no such order
        - `ownership_mismatch` (403) — the order belongs to someone else.
          Do not retry with a different `customer_id`; ask the customer.
    """
    if order_id.startswith("#"):
        order_id = order_id[1:]
    o, err = _load_owned_order(customer_id, order_id)
    if err is not None:
        return err
    return o.model_dump(exclude={"customer_id"})


_OPEN_ORDER_STATUSES = {"placed", "preparing", "in_transit", "in_transit_delayed"}


def _order_context(order) -> dict:
    """Return the useful decision context without dumping the backend model.

    The list tool is often the model's only observation before it selects an
    order, so each row must be sufficient for the demo cases.  At the same
    time, fields such as payment_method and raw photo filenames add tokens and
    can pull attention away from the customer's item reference.
    """
    result = {
        "order_id": order.order_id,
        "items": order.items,
        "status": order.status,
        "placed": order.placed,
        "total_usd": order.total_usd,
    }

    # Delivery state is useful for the missing-order, late-order, memory, and
    # address-change demos.  The current address matters only while an order
    # can still be delivered or intercepted.
    if order.status in _OPEN_ORDER_STATUSES:
        result["shipping_address"] = order.shipping_address
    if order.carrier:
        result["carrier"] = order.carrier
    if order.tracking_id:
        result["tracking_id"] = order.tracking_id
    if order.estimated_delivery:
        result["estimated_delivery"] = order.estimated_delivery
    if order.delivery_days_late or order.status == "in_transit_delayed":
        result["delivery_days_late"] = order.delivery_days_late

    # Eligibility needs the presence of evidence, not opaque storage names.
    # Keep this on the matched order so the agent does not need a second read.
    if order.damaged or order.status == "delivered_damaged":
        result["damage_evidence"] = {
            "photos_on_file": bool(order.damage_photos),
            "photo_count": len(order.damage_photos),
        }

    return result


@mcp.tool()
def get_customer_orders(customer_id: str, limit: int = 10) -> dict:
    """List a customer's recent orders, newest first, with enough context to act.

    The usual first call, and normally the only read you need: each row carries
    what it takes to both identify the order and judge eligibility, so calling
    `get_order` afterwards for the same fields is a wasted round trip.

    Match the customer's own reference — order ID, item description, or date —
    to a row before considering eligibility. Never substitute a different order
    because its state or evidence makes the action easier. If no row matches
    uniquely, ask the customer which they mean.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).
        limit: Maximum rows to return (default 10).

    Returns:
        `{"orders": [...], "returned": int, "total": int, "truncated": bool}`.
        Each row has `order_id`, `items`, `status`, `placed` and `total_usd`,
        plus, where they apply, `shipping_address` (only while the order can
        still be redirected), `carrier`, `tracking_id`, `estimated_delivery`,
        `delivery_days_late`, and `damage_evidence` with `photos_on_file`.
        When `truncated` is true, raise `limit` before concluding an order
        is absent.
    """
    items = _client.get_customer_orders(customer_id)
    items.sort(key=lambda o: o.placed, reverse=True)
    shown = items[:limit]
    # Report the truncation instead of hiding it. A list that looks complete
    # but silently drops the order the customer is asking about sends the
    # model looking for the closest match among the rows it can see.
    return {
        "orders": [_order_context(o) for o in shown],
        "returned": len(shown),
        "total": len(items),
        "truncated": len(items) > len(shown),
    }


@mcp.tool()
def get_open_tickets(customer_id: str) -> dict:
    """List the customer's open support tickets.

    Check before calling `escalate_to_human`, so an issue a human is already
    working is not filed twice, and when the conversation refers back to a
    ticket the customer has been given.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).

    Returns:
        `{"tickets": [...], "count": int}`. An empty list means nothing is open.
    """
    tickets = _client.get_open_tickets(customer_id)
    return {"tickets": tickets, "count": len(tickets)}


@mcp.tool()
def get_refund_history(customer_id: str) -> dict:
    """List refunds already issued to this customer.

    Call before any refund. Entitlements are per order and cumulative: a
    customer owed 50% who has already had 20% back is owed 30% now, not 50%.
    Skipping this is how an agent over-refunds.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).

    Returns:
        `{"refunds": [...], "count": int}`, each entry carrying the order,
        amount, `refund_percentage`, reason and reference.
    """
    refunds = _client.get_refund_history(customer_id)
    return {"refunds": refunds, "count": len(refunds)}


# ----- Write tools ---------------------------------------------------


@mcp.tool()
def update_shipping_address(customer_id: str, order_id: str, new_address: str) -> dict:
    """Redirect an order that has not shipped yet to a new address.

    Only `placed` and `preparing` orders can be redirected. Once an order is
    in transit the carrier owns the delivery and this call fails.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).
        order_id: The order's ID, e.g. "1234".
        new_address: The complete replacement address as one string. It
            overwrites the old one, so send the whole address, not a fragment.

    Returns:
        `{"ok": true, "ref": "..."}`. On failure:
        - `order_already_shipped` — carries `current_status` and
          `remediation: redirect_to_carrier`; tell the customer to contact
          the carrier rather than retrying
        - `ownership_mismatch` (403) / `order_not_found`
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
    """Cancel an order that has not shipped yet.

    Only `placed` and `preparing` orders can be cancelled.

    This does NOT refund. If the customer has been charged, follow up with
    `issue_refund` using `reason_code="cancellation"` — which this call must
    succeed first for, since that code is rejected on an order that is not yet
    cancelled. The returned `next_step` repeats the rule at the point of use.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).
        order_id: The order's ID, e.g. "1234".
        reason: Short free text: why the customer wants to cancel. Recorded
            on the order.

    Returns:
        `{"ok": true, "ref": "...", "next_step": "..."}`. On failure:
        - `order_already_shipped` — carries `current_status` and
          `remediation: contact_customer_for_return`; this becomes a return,
          not a cancellation
        - `ownership_mismatch` (403) / `order_not_found`
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


# Every refund has to name the entitlement it is claiming, and every
# entitlement is backed by a fact this backend already stores. That is what
# makes the damaged-item photo rule checkable: the server never has to decide
# whether a request "is a damage claim" (an intent classification it cannot do
# soundly), it only has to verify the claim the caller made. An agent that
# picks the wrong code to dodge a requirement just fails that code's own check.
#
# The codes are the categories in the `refund_calculation` policy table, no
# more. There is deliberately no open "goodwill" code: one code without an
# evidence requirement would be a hole big enough to drive every other claim
# through, since an agent blocked on `damaged` could simply re-claim the same
# refund under it.
REFUND_REASON_CODES = ("damaged", "cancellation", "shipping_delay_credit", "return")


def _refund_entitlement_error(order, reason_code: str) -> dict | None:
    """Return a `policy_violation` body when stored facts do not support the
    claimed reason code, or None when the entitlement holds."""
    if reason_code == "damaged":
        if not order.damaged:
            return {
                "detail": (
                    f"order {order.order_id} is not recorded as damaged, so a "
                    "damaged-item refund does not apply"
                ),
                "remediation": "Re-check the order status and use the reason code that matches it.",
            }
        if not order.damage_photos:
            return {
                "detail": (
                    f"order {order.order_id} has no damage photos on file; the "
                    "damaged-item policy requires photo evidence before any refund"
                ),
                "remediation": (
                    "Ask the customer to send photos of the damage and escalate the "
                    "missing-photo exception at normal priority."
                ),
            }
    elif reason_code == "cancellation":
        # `refund_calculation`: a cancellation refund is allowed only after the
        # cancellation succeeds. Checking the status here makes the skills'
        # cancel-before-refund ordering rule enforceable rather than advisory.
        if order.status != "cancelled":
            return {
                "detail": (
                    f"order {order.order_id} is {order.status}, not cancelled; a "
                    "cancellation refund is only allowed after the cancellation succeeds"
                ),
                "remediation": "Call cancel_order first and refund only once it returns successfully.",
            }
    elif reason_code == "shipping_delay_credit":
        # `shipping_delay`: 3 business days or less does not qualify.
        if order.delivery_days_late <= 3:
            return {
                "detail": (
                    f"order {order.order_id} is {order.delivery_days_late} day(s) late; "
                    "shipping-delay credit requires more than 3 days"
                ),
                "remediation": "Check delivery_days_late on the order before claiming this code.",
            }
    elif reason_code == "return":
        # `return_window`: damage claims follow `damaged_item` regardless of the
        # window, so a damaged order cannot be refunded as a plain return.
        #
        # Known gap: the policy also requires the customer to have confirmed the
        # item was shipped back, and this backend stores nothing about return
        # shipments. That half of the rule is unenforceable here, so `return` is
        # the one code whose check is incomplete. Recording return shipments
        # would close it.
        if order.damaged:
            return {
                "detail": (
                    f"order {order.order_id} is recorded as damaged, so it follows the "
                    "damaged-item policy rather than the return window"
                ),
                "remediation": "Use reason_code=\"damaged\" and satisfy the photo-evidence requirement.",
            }
    return None


@mcp.tool()
def issue_refund(
    customer_id: str,
    order_id: str,
    refund_percentage: float,
    reason_code: str,
    reason: str,
) -> dict:
    """Refund part or all of an order's value. Moves real money — read the arguments.

    Settle the amount before calling. Subtract any earlier refund on the same
    order (`get_refund_history`): entitlements are cumulative per order, so a
    50% entitlement against a prior 20% refund is a 30% call, not 50%.

    Args:
        customer_id: The customer's ID, e.g. "cust_001" ("" under a binding harness).
        order_id: The order's ID, e.g. "1234".
        refund_percentage: A FRACTION in (0, 1] — `0.25` is 25%, `1.0` is the
            full order. Passing `25` for 25% is rejected as `invalid_pct`.
        reason_code: The entitlement being claimed. Not a label: the server
            checks it against the order's stored facts and refuses when they
            disagree. One of —
            - `"damaged"`: order recorded damaged AND photos already on file
            - `"cancellation"`: order already cancelled (cancel first)
            - `"shipping_delay_credit"`: more than 3 days late
            - `"return"`: anything not a damage claim
            There is deliberately no catch-all. If none of the four fits, the
            refund has no policy basis — escalate rather than claiming the
            nearest code; a wrong code just fails that code's own check.
        reason: Short free text for the audit record.

    Returns:
        `{"ok": true, "ref", "reason_code", "refund_percentage", "amount_usd"}`.
        On failure:
        - `invalid_pct` — not a fraction in (0, 1]
        - `invalid_reason_code` — carries the `allowed` list
        - `policy_violation` (403) — over this agent's refund cap. No evidence
          makes it permissible at this size; `remediation: escalate_to_human`
        - `policy_violation` (422) — stored facts contradict the claimed code;
          `detail` says which fact and `remediation` what to do about it
        - `service_timeout` (504) — `outcome: not_committed`,
          `retryable: false`. Do NOT retry: escalate at high priority, and
          only tell the customer once that ticket succeeds
        - `ownership_mismatch` (403) / `order_not_found`
    """
    o, err = _load_owned_order(customer_id, order_id)
    if err is not None:
        return err

    if reason_code not in REFUND_REASON_CODES:
        return {
            "error": "invalid_reason_code",
            "allowed": list(REFUND_REASON_CODES),
        }

    if not isinstance(refund_percentage, (int, float)) or refund_percentage <= 0 or refund_percentage > 1:
        return {
            "error": "invalid_pct",
            "detail": (
                f"refund_percentage must be a fraction in (0, 1] (e.g. 0.10 for 10%), "
                f"got {refund_percentage!r}"
            ),
        }

    amount_usd = round(float(refund_percentage) * float(o.total_usd), 2)

    # The cap is checked before the entitlement so that an over-cap refund
    # always surfaces as the authority failure, whatever code was claimed. The
    # two only overlap on orders that fail both, and for those the cap is the
    # harder limit: no evidence would make the refund permissible at this size.
    allowed, why = _identity.can_refund(amount_usd)
    if not allowed:
        return {
            "error": "policy_violation",
            "code": 403,
            "detail": why,
            "remediation": "escalate_to_human",
        }

    entitlement_error = _refund_entitlement_error(o, reason_code)
    if entitlement_error is not None:
        return {
            "error": "policy_violation",
            "code": 422,
            "reason_code": reason_code,
            **entitlement_error,
        }

    if consume_fault(_identity.agent_id, "refund_service_timeout"):
        # Normalize the downstream transport failure at the MCP boundary. A
        # raised Python exception would be flattened by MCP/Strands into an
        # unstructured "Error executing tool" string. This structured result
        # gives the agent safe, explicit recovery semantics.
        return {
            "error": "service_timeout",
            "code": 504,
            "outcome": "not_committed",
            "retryable": False,
            "detail": "The refund service timed out before accepting the write.",
            "remediation": "escalate_to_human",
            "agent_instruction": (
                "Do not retry. Call escalate_to_human with priority high and explain that "
                "the refund service timed out before the write was accepted. Only after the "
                "ticket succeeds may you tell the customer it was escalated."
            ),
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
        "reason_code": reason_code,
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
    """Hand the case to a human by opening a support ticket.

    The correct move whenever the request exceeds your authority, the stored
    facts support no available action, a write failed unrecoverably, the
    customer is adversarial, or they simply ask for a person. Escalating is
    not a failure; guessing past a policy limit is.

    Check `get_open_tickets` first so an issue already being worked is not
    filed twice.

    Args:
        reason: What is unresolved and why a human is needed. Customer-facing
            facts only — no internal reasoning, tool traces, or data the
            ticket does not need.
        priority: `"low"`, `"normal"`, `"high"` or `"urgent"` (default
            `"normal"`). Use `"high"` for a failed write or a risky case.
        customer_id: The customer's ID, or None when the issue is not tied
            to one customer.

    Returns:
        `{"ok": true, "ticket_id": "...", "priority": "..."}`, or
        `invalid_priority` with the `allowed` list. Do not tell the customer
        they have been escalated until this has returned ok.
    """
    if priority not in {"low", "normal", "high", "urgent"}:
        return {
            "error": "invalid_priority",
            "allowed": ["low", "normal", "high", "urgent"],
        }
    ticket_id = _client.open_ticket(reason, priority, _identity.agent_id, customer_id)
    return {"ok": True, "ticket_id": ticket_id, "priority": priority}


if __name__ == "__main__":
    # stdio by default — that is what agent/core.py spawns. Pass
    # `--transport http` to serve the same tools over the network instead.
    raise SystemExit(serve(mcp, "customer_support_engineered"))
