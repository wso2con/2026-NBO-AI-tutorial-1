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
from mocks.client import CustomerSupportClient, consume_fault

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
    """Look up the current customer's profile.

    Use when you need customer details such as name, tier, verification
    status, or email.

    The customer is bound by the system; pass "" for `customer_id`.

    Returns the customer record or `{"error": "customer_not_found"}`.
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
    """Fetch one order belonging to the current customer.

    Use when you know the order ID and need its status, items, total,
    shipping address, delivery delay, or damage status.

    `damage_photos` lists the damage photos already on file for this order.
    An empty list means no photo evidence has been filed yet.

    The customer is bound by the system; pass "" for `customer_id`.
    Ownership is verified server-side.

    Returns `ownership_mismatch` if the order does not belong to the
    current customer. Do not retry with a different customer.
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
    """List the current customer's recent orders as compact, complete case context.

    Use when the customer has not provided an order ID or when order
    history is relevant. Match the customer's order ID, item description, or
    date to a row before considering eligibility. Never select a different
    order merely because its state or evidence makes the requested action
    easier. If no row matches uniquely, ask the customer to clarify.

    Each row contains the fields needed for the order-status, damage, refund,
    memory, and address-change demos. Do not call `get_order` merely to fetch
    the same fields again.

    The customer is bound by the system; pass "" for `customer_id`.

    The result includes `total` and `truncated`. If `truncated` is true,
    increase `limit` before concluding an order is not present.
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
    """Return open support tickets for the current customer.

    Use before escalating an issue that may already be tracked, or when
    previous context refers to an existing ticket.

    The customer is bound by the system; pass "" for `customer_id`.
    """
    tickets = _client.get_open_tickets(customer_id)
    return {"tickets": tickets, "count": len(tickets)}


@mcp.tool()
def get_refund_history(customer_id: str) -> dict:
    """Return refunds previously issued to the current customer.

    Use when prior refunds are relevant to the current request.

    Each entry includes the order, refunded amount, refund percentage,
    reason, and reference.

    The customer is bound by the system; pass "" for `customer_id`.
    """
    refunds = _client.get_refund_history(customer_id)
    return {"refunds": refunds, "count": len(refunds)}


# ----- Write tools ---------------------------------------------------


@mcp.tool()
def update_shipping_address(customer_id: str, order_id: str, new_address: str) -> dict:
    """Update the shipping address of an eligible order.

    Use after you have the order and the new address required for the change.

    `reason` should briefly explain why the address is being changed.
    The customer is bound by the system; pass "" for `customer_id`.

    Returns `order_already_shipped` if the address can no longer be changed.
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
    """Cancel an eligible order.

    `reason` should briefly explain why the customer wants the cancellation.
    The customer is bound by the system; pass "" for `customer_id`.

    This tool only cancels the order; it does not issue a refund.

    Returns `order_already_shipped` if the order can no longer be cancelled.
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
        # `return_window`: damage claims follow `refund_damaged_item` regardless of the
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
    """Issue a refund for an order as a percentage of its total value.

    `refund_percentage` must be a fraction in `(0, 1]`:
    `0.25` means 25% and `1.0` means a full refund.

    Determine the appropriate refund percentage before calling this tool.

    `reason_code` names the entitlement being claimed and is checked against
    the order's stored facts. It must be one of:
    - `damaged`: order must be recorded as damaged AND have damage photos on file
    - `cancellation`: order must already be cancelled
    - `shipping_delay_credit`: order must be more than 3 days late
    - `return`: order must not be a damage claim

    There is no catch-all code. If no code fits, the refund is not permitted
    under policy: escalate instead of picking the nearest one.

    `reason` is free text describing why the refund is being issued.

    The customer is bound by the system; pass "" for `customer_id`.

    On success, returns the refund reference, percentage, and dollar amount.

    Important errors:
    - `invalid_pct`: invalid refund percentage
    - `invalid_reason_code`: unknown reason code
    - `ownership_mismatch`: order does not belong to the customer
    - `policy_violation`: refund is not permitted; follow its remediation
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
    """Open a support ticket for human handling.

    Use when human judgment or intervention is required, or when the
    customer explicitly requests a human.

    `reason` should briefly describe the customer's unresolved issue and
    why it needs human handling. Do not include internal reasoning,
    tool traces, or unnecessary customer data.

    `priority` must be one of: `low`, `normal`, `high`, `urgent`.

    The customer is bound by the system; pass "" for `customer_id`.
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
