"""Strands hooks for the customer-support agent.

Three harness-side gates, all of them things the model cannot talk its way
past: `CustomerIdBindingHook` (who the caller is), `RefundCapHook` (how much
the agent may spend) and `HumanConfirmationHook` (which writes need a human's
yes first).

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
import re
from dataclasses import dataclass

from strands.hooks import HookRegistry
from strands.hooks.events import BeforeToolCallEvent

# Tools whose input dicts get `customer_id` injected. Anything NOT in this set
# is left alone — e.g., `check_policy` (no customer scoping) or the
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
    the cap. The harness and the engineered MCP subprocess each carry their own
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
        # We hand back JSON matching the engineered server's 403 shape so the
        # SSE trace renderer recognises it and shows a clean red error.
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


@dataclass
class RefundEvidenceHook:
    """Enforce per-reason-code refund entitlements at the harness — defense-in-depth above the MCP server's own check.

    `issue_refund` takes a `reason_code` naming the entitlement being claimed.
    Each code is backed by a fact already stored on the order, so the harness
    never has to decide whether a request "is a damage claim" — an intent
    classification it cannot do soundly. It only verifies the claim the model
    actually made, and an agent that switches codes to dodge one requirement
    lands on another code's check instead.

    This matters most for the damaged-item photo rule. A post-turn reviewer
    can identify a bad refund but cannot undo it; rejecting the call here is
    the control that actually protects the customer.

    Order scoping is why the hook re-reads the selected order. A customer can
    have both photographed and unphotographed damaged orders, so a run-level
    observation cannot safely authorize a specific refund.
    """

    agent_id: str

    def __post_init__(self) -> None:
        from mocks.client import CustomerSupportClient
        self._client = CustomerSupportClient(agent_id=self.agent_id)

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._enforce_entitlement)

    def _enforce_entitlement(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        if tool_use.get("name") != "issue_refund":
            return
        inputs = tool_use.get("input") or {}
        reason_code = inputs.get("reason_code")
        order_id = inputs.get("order_id")
        if not isinstance(reason_code, str) or not order_id:
            return  # let the server return the structured invalid_reason_code error
        # Writes made by the MCP subprocess this session land on the same disk
        # dir, so refresh before reading, exactly as the cap hook does.
        self._client._reload_cache()
        order = self._client.get_order(order_id)
        if order is None:
            return  # let the server return ownership_mismatch / not_found

        detail: str | None = None
        remediation = ""
        if reason_code == "damaged":
            if not order.damaged:
                detail = f"order {order_id} is not recorded as damaged"
                remediation = "Use the reason code that matches the order's recorded status."
            elif not order.damage_photos:
                detail = (
                    f"order {order_id} has no damage photos on file; the damaged-item "
                    "policy requires photo evidence before any refund"
                )
                remediation = (
                    "Ask the customer to send photos of the damage and escalate the "
                    "missing-photo exception at normal priority."
                )
        elif reason_code == "cancellation" and order.status != "cancelled":
            detail = (
                f"order {order_id} is {order.status}, not cancelled; a cancellation "
                "refund is only allowed after the cancellation succeeds"
            )
            remediation = "Call cancel_order first and refund only once it returns successfully."
        elif reason_code == "shipping_delay_credit" and order.delivery_days_late <= 3:
            detail = (
                f"order {order_id} is {order.delivery_days_late} day(s) late; "
                "shipping-delay credit requires more than 3 days"
            )
            remediation = "Check delivery_days_late on the order before claiming this code."
        elif reason_code == "return" and order.damaged:
            detail = (
                f"order {order_id} is recorded as damaged, so it follows the "
                "damaged-item policy rather than the return window"
            )
            remediation = 'Use reason_code="damaged" and satisfy the photo-evidence requirement.'
        if detail is None:
            return

        # Same 422 shape the engineered server returns, so the SSE trace
        # renderer shows one consistent error whichever layer caught it.
        event.cancel_tool = json.dumps(
            {
                "error": "policy_violation",
                "code": 422,
                "reason_code": reason_code,
                "detail": detail,
                "remediation": remediation,
            }
        )


# ---------------------------------------------------------------------------
# Human confirmation before customer-visible writes
# ---------------------------------------------------------------------------

# Tools that change the customer's world in a way they will notice and cannot
# undo themselves. Reads are free; these are not. One hook covers all of them
# so the gate can't be forgotten when the next write tool is added — put the
# tool name in this set and it inherits the confirmation step.
#
# `issue_refund` is deliberately NOT here: it only ever runs as the tail of a
# cancellation the customer already approved, and asking twice in one flow
# trains people to say yes without reading. Add it to the set if that changes.
CONFIRM_BEFORE_TOOLS = frozenset(
    {
        "update_shipping_address",
        "cancel_order",
    }
)

_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|confirmed?|confirm|approved?|approve|proceed|"
    r"go ahead|do it|please do|ok|okay|sure)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|nope|don'?t|do not|stop|wait|hold on|not yet|never ?mind|"
    r"cancel that|leave it)\b",
    re.IGNORECASE,
)


def is_affirmative(text: str | None) -> bool:
    """Did the human say yes?

    The harness — not the model — reads the customer's answer, for the same
    reason `CustomerIdBindingHook` binds identity outside the prompt: an
    approval the LLM grants itself is not an approval. A negative anywhere in
    the reply wins ("yes I want a refund, but no, don't cancel it" is a no),
    and anything that is neither reads as "not approved".
    """
    if not text:
        return False
    if _NEGATIVE.search(text):
        return False
    return bool(_AFFIRMATIVE.search(text))


def _public_args(inputs: dict) -> dict:
    """Tool args worth showing the customer — `customer_id` is harness-owned noise."""
    return {k: v for k, v in inputs.items() if k != "customer_id"}


def confirmation_question(tool_name: str, inputs: dict) -> str:
    """The exact question the customer is asked, built by the harness.

    Written here rather than left to the model so the wording states what is
    actually about to happen, with the real arguments in it. A model-authored
    "shall I go ahead?" can describe an action other than the one queued.

    This is the fallback wording. A console that renders `confirmation_card`
    shows the structured form instead; both are built from the same args.
    """
    order_id = inputs.get("order_id", "this order")
    if tool_name == "cancel_order":
        action = f"cancel order #{order_id}"
    elif tool_name == "update_shipping_address":
        action = (
            f"change the shipping address on order #{order_id} to "
            f"\"{inputs.get('new_address', '')}\""
        )
    else:
        action = f"run {tool_name} with {_public_args(inputs)}"
    # No "reply yes/no" instruction: the console renders the decision as
    # buttons from the turn's `pause` block, and a typed answer is read by
    # `is_affirmative` either way. The question only has to state exactly what
    # is about to happen.
    return f"Before I {action}, I need your confirmation. Shall I go ahead?"


def _titlecase_label(key: str) -> str:
    """`new_address` -> `New address`, for tools without hand-written wording."""
    return key.replace("_", " ").strip().capitalize()


def confirmation_card(tool_name: str, inputs: dict) -> dict:
    """A structured description of the one write being authorised.

    A console needs more than a sentence to render a consent control a person
    can actually check: a title for what the action is, the specific values it
    will use, and a plain line about what changes if they say yes. Built here,
    from the same `tool_use["input"]` the tool will receive, so what the
    customer approves and what runs cannot drift — a card assembled in the
    frontend out of prose would be a second, unverified description of the
    action.

    Unknown tools fall back to their raw arguments rather than being hidden: a
    write that has not been given wording yet must still be legible before it
    is approved.
    """
    args = _public_args(inputs)
    order_id = str(inputs.get("order_id", "") or "")
    if tool_name == "cancel_order":
        return {
            "title": "Cancel this order",
            "fields": [{"label": "Order", "value": f"#{order_id}"}],
            "effect": "The order stops shipping and cannot be un-cancelled from here.",
        }
    if tool_name == "update_shipping_address":
        return {
            "title": "Change the delivery address",
            "fields": [
                {"label": "Order", "value": f"#{order_id}"},
                {"label": "New address", "value": str(args.get("new_address", ""))},
            ],
            "effect": "This order ships to the new address instead of the old one.",
        }
    return {
        "title": f"Run {tool_name.replace('_', ' ')}",
        "fields": [
            {"label": _titlecase_label(k), "value": str(v)} for k, v in args.items()
        ],
        "effect": "This changes the customer's account.",
    }


@dataclass
class HumanConfirmationHook:
    """Pause the loop and ask the customer before any write in `CONFIRM_BEFORE_TOOLS`.

    Strands' interrupt mechanism does the pausing: `event.interrupt(...)` raises
    out of the event loop the first time it is called, so the tool never runs
    and the turn ends with `stop_reason == "interrupt"`. `main.py` streams the
    question to the browser, remembers the interrupt id, and resumes the same
    agent on the next message with the customer's answer. The second call to
    `event.interrupt(...)` — on resume — returns that answer instead of raising.

    The gate lives in the harness rather than in a tool docstring or a skill
    because both of those are advice to the model. A prompt-injected or simply
    careless model can skip advice; it cannot skip a `BeforeToolCallEvent`
    callback. The model proposes the write, the human authorises it, the
    harness enforces that ordering.

    Denial is reported back as a normal tool error, so the model keeps its turn
    and can answer whatever the customer actually said instead.

    One interrupt per queued call, never one per turn: Strands scopes the
    interrupt id to the `toolUseId`, so two `update_shipping_address` calls
    issued in the same model turn park as two separate decisions the customer
    answers one at a time. Batching them behind a single yes would make the
    second write ride in on the first one's approval.
    """

    tools: frozenset[str] = CONFIRM_BEFORE_TOOLS

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._require_confirmation)

    def _require_confirmation(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        name = tool_use.get("name")
        if name not in self.tools:
            return
        if event.cancel_tool:
            # An earlier hook (identity binding, refund cap) already rejected
            # this call. Don't ask the customer to approve something that is
            # not going to run either way.
            return
        inputs = tool_use.get("input") or {}

        # First pass: raises InterruptException, the loop stops, the tool does
        # not run. On resume, the same call returns the harness's answer.
        answer = event.interrupt(
            f"confirm_{name}",
            reason={
                "kind": "human_confirmation",
                "tool": name,
                # The call this decision gates. The console renders the consent
                # control against that trace row, so the question sits with the
                # action it is about instead of floating at the end of the turn.
                "tool_use_id": tool_use.get("toolUseId") or tool_use.get("id") or "",
                "args": _public_args(inputs),
                "question": confirmation_question(name, inputs),
                "card": confirmation_card(name, inputs),
            },
        )

        approved = bool(answer.get("approved")) if isinstance(answer, dict) else bool(answer)
        if approved:
            return

        customer_reply = ""
        if isinstance(answer, dict):
            customer_reply = str(answer.get("customer_reply") or "").strip()
        event.cancel_tool = json.dumps(
            {
                "error": "confirmation_denied",
                "code": 409,
                "detail": f"the customer did not approve {name}; nothing was changed",
                "customer_reply": customer_reply,
                "remediation": (
                    "do not retry this call. Acknowledge that nothing was changed "
                    "and respond to what the customer actually asked for."
                ),
            }
        )
