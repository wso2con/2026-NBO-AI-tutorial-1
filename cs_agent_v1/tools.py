"""Tools for cs_agent_v1 — REALISTIC bad design.

Every tool here is shaped like real internal-API code: clean function
signatures, typed parameters, working error paths. But the API was
designed by a backend team that wasn't thinking about an LLM consumer.
These are realistic shapes you'll find in any company's CRM/orders
codebase that grew organically over a few years — overlapping tools
from different teams' namespaces, undocumented dict payloads, raw DB
records leaking internal metadata, atomic micro-getters, and errors
that come back as free-text strings with no codes or remediation hints.

The 5 sections below — `# === Anti-pattern N: ... ===` — name each
realistic anti-pattern and the harm it causes when an LLM tries to
use the tool. None of these are cartoonish; they're real production
code that just wasn't designed for an agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Shared mocks/ package lives at the lab root (one dir up).
_LAB_ROOT = Path(__file__).parent.parent
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from strands import tool

from config import AGENT_ID, REFUND_CAP_USD
from mocks.client import CustomerSupportClient

# v1 has no scoped identity — no AgentIdentity dataclass, no per-call
# principal, no harness-enforced cap. The agent_id is a string the
# write tools stamp onto ledger entries, and the cap is a number
# checked inline below. Both come from `config.py` (env-overridable).

_client = CustomerSupportClient(agent_id=AGENT_ID)


def reset_state() -> None:
    """Wipe the in-memory mock backend and reload seeds. Called by
    main.py's /api/reset endpoint."""
    _client.reset()


# === Anti-pattern 1: Overlapping / similar descriptions ====================
# Two tools whose names + docstrings sound interchangeable. Both are about
# "orders." The CRM team's `get_order` takes an order_id; the same team
# also shipped `get_customer_orders` which takes a customer_id and returns
# everything they own. A human reader can tell from the signature which
# is which; the LLM, reading vague one-liners, often guesses wrong — calls
# `get_order` with a customer_id, or `get_customer_orders` and then loops
# to filter for a specific order client-side. Either way it dithers.


@tool
def get_order(order_id: str) -> dict:
    """Look up an order."""
    o = _client.get_order(order_id)
    if o is None:
        # Backend failure shape: the same generic 5xx-style envelope the
        # legacy order service returns for ANY failure — missing record,
        # database hiccup, permissions issue, malformed request. The agent
        # has no signal what actually went wrong. Recovery becomes
        # guesswork: retry? ask the user? escalate? v2's MCP server tells
        # the agent specifically `{"error": "order_not_found", "order_id":
        # "..."}` and the agent acts on it; v1 leaves the agent stuck.
        return {"error": "internal server error"}
    return o.model_dump()


@tool
def get_customer_orders(customer_id: str) -> list[dict]:
    """Look up the customer's orders."""
    return [o.model_dump() for o in _client.get_customer_orders(customer_id)]


# === Anti-pattern 2: Bad input schema — overloaded "god" tool =============
# `modify_order` takes flat optional params for three unrelated operations
# — cancel (status+reason), refund (refund_percentage+reason), and address
# change (shipping_address) — all behind one tool. Typed and clean-looking
# on the signature, but the tool guesses which operation the caller meant
# from which subset of params is set. Pass `status="cancelled"` AND
# `refund_percentage=0.9` and only one wins silently; pass nothing
# actionable and the call no-ops with a free-text "nothing to do".
# Stripe's `SubscriptionUpdate`, Salesforce's `Account update`, any
# "PATCH /resource" with 30 optional fields — realistic shape, terrible
# for an LLM that has to pick a valid combination from the schema alone.
#
# The refund branch takes `refund_percentage` (a fraction of the order's
# total). The docstring is silent on how to choose the percentage — no
# mention of the `refund_calculation` policy, no mention of subtracting
# prior refunds on the same order. An LLM with no skill / no policy
# nudge typically passes the category percentage from memory and
# over-refunds whenever a prior credit (e.g. shipping-delay) already
# exists on the order.


@tool
def modify_order(
    order_id: str,
    status: str | None = None,
    reason: str | None = None,
    refund_percentage: float | None = None,
    shipping_address: str | None = None,
) -> dict:
    """Modify an order: cancel, refund, or update shipping address. For refunds send refund_percentage."""
    o = _client.get_order(order_id)
    if o is None:
        return {"status": "failed", "message": f"order {order_id} not found"}

    if status == "cancelled":
        if o.status == "in_transit":
            return {"status": "failed"}
        ref = _client.cancel_order(order_id, reason or "no reason", AGENT_ID)
        return {"status": "ok", "ref": ref}

    if refund_percentage is not None:
        # AP2 + AP4: docstring is silent on how to pick the percentage. No
        # mention of the refund_calculation policy, no mention of subtracting
        # prior refunds. Agent typically passes the category percentage from
        # memory and over-refunds when a shipping-delay credit already exists.
        amt = float(refund_percentage) * float(o.total_usd)
        ref = _client.issue_refund(
            order_id,
            o.customer_id,
            amt,
            reason or "refund",
            AGENT_ID,
            refund_percentage=float(refund_percentage),
        )
        return {"status": "ok", "ref": ref, "amount_usd": amt}

    if shipping_address is not None:
        ref = _client.update_shipping_address(
            order_id, shipping_address, AGENT_ID
        )
        return {"status": "ok", "ref": ref}

    # AP2: nothing actionable was set. Agent may have passed only
    # `reason=...` without any operation field, or set `status` to
    # something other than "cancelled". Free-text only, no hint about
    # which combination of params actually triggers work.
    return {"status": "failed", "message": "no recognised changes in payload"}


# === Anti-pattern 3: Output noise — legacy SOAP-style envelopes ============
# `get_refund_history` and `get_open_tickets` are wired through the CRM's
# legacy SOAP-to-JSON adapter. Every response comes back wrapped in XML-
# attribute-style keys (`@xmlns`, `@xsi:type`), with a CDATA-wrapped
# `ReasonText`, plus deprecation hints, audit pointers, replica lag, ACLs,
# legacy priority codes. The fields the agent actually wants are buried
# four levels deep behind ~10 noise keys. v2's typed MCP versions return
# the same data as a flat list with action-named fields.


@tool
def get_refund_history(customer_id: str) -> dict:
    """Get refund history."""
    return _client.get_refund_history_legacy(customer_id)


@tool
def get_open_tickets(customer_id: str) -> dict:
    """Get open tickets."""
    return _client.get_open_tickets_legacy(customer_id)


# === Anti-pattern 4: Bad error handling — free-text, no recovery, inconsistent
# Errors are scattered across the surface with different shapes and no
# signals the agent can branch on:
#   - `get_order` missing          → {"error": "order not found"}
#   - `modify_order` failure       → {"status": "failed", "message": "..."}
#   - `get_customer_orders` empty  → [] (indistinguishable from "real customer with no orders")
#   - `escalate`                   → always "escalated", whether or not
#                                    the ticket actually opened
# No `code`, no `remediation`, no way to tell transient from permanent.
# The agent has to learn every tool's quirks individually.


# === Anti-pattern 5: Tools too atomic ======================================
# Splitting one logical lookup into many micro-tools forces the agent to
# round-trip multiple times for what should be one call. Real teams build
# these for "minimal data exposure" or "fine-grained REST endpoints"; the
# LLM consumer pays for it in latency and drift.


@tool
def get_customer_name(customer_id: str) -> str:
    """Get the name."""
    c = _client.get_customer(customer_id)
    return c.name if c else ""


@tool
def get_customer_email(customer_id: str) -> str:
    """Get the email."""
    c = _client.get_customer(customer_id)
    return c.email if c else ""


@tool
def get_customer_tier(customer_id: str) -> str:
    """Get the tier."""
    c = _client.get_customer(customer_id)
    return c.tier if c else ""


@tool
def get_customer_verified(customer_id: str) -> bool:
    """Is verified."""
    c = _client.get_customer(customer_id)
    return bool(c.verified) if c else False


# === Utility tools =========================================================
# Not isolated to any single anti-pattern but exhibit AP4 (bad errors)
# in their own ways.
#
# `search_kb` delegates to the same `policies.search.search` function v2's
# `search_policy_kb` calls — identical corpus, identical scoring, identical
# return shape. The §6 demo (Plan before commit) is deliberately not about
# retrieval quality differences. What differs between v1 and v2 is the
# framing: v1 exposes the search behind a one-line docstring and has no
# skill / procedure pushing the agent to consult it before write actions.
# Same evidence on the table; v2's harness makes "consult policy first"
# the path of least resistance — v1's says the same words in the prompt
# and the LLM still commits directly.


from policies.search import search as _policy_search  # noqa: E402


@tool
def search_policy_kb(query: str) -> list[dict]:
    """Search the policyknowledge base."""
    return _policy_search(query)


# Escalation — minimally usable. No `priority` parameter, no
# `customer_id` link, and ALWAYS returns "escalated" regardless of
# whether the ticket actually opened. Realistic for a first-cut
# escalation endpoint that nobody added richer signals to.


@tool
def escalate(reason: str) -> str:
    """Escalate to a human."""
    _client.open_ticket(reason, "normal", AGENT_ID, None)
    return "escalated"
