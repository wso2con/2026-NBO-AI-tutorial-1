---
id: refund_authority
title: Refund authority limits
keywords: [refund, refund limit, high value, approval, escalate, cap]
---

# Refund authority limits

AI agents may issue refunds up to their scoped cap (the `refund_cap_usd` field
in their `agent-profile.yaml`; default $200). Refunds above the cap **MUST** be
escalated to a human agent with priority `normal` or higher.

## Anti-split rule (audit-flagged)

Splitting a single refund event into multiple smaller refunds to dodge the cap
is **explicitly prohibited**. If a customer needs $1,600 refunded for damaged
chairs, the agent must escalate the full $1,600 — not issue 8 calls of $200.

Splits are detected in the audit ledger (multiple refunds on the same order_id
within a short window) and flagged as policy violations.

## Agent action

Before issuing any non-trivial refund, mentally check:
- Is this above my cap? → escalate with the FULL amount, do not attempt.
- Have I already issued a refund on this order this session? → check the
  customer's refund history and the audit context before double-refunding.

If a refund attempt returns a `policy_violation` (code 403) with a
`remediation` of `escalate_to_human`, that's the cap rejecting the call.
This is a **permanent** error — do NOT retry with smaller amounts. Escalate.

## Escalation context to include

When escalating, the human picking up the ticket should not need to
re-investigate. Include:
- Customer ID + name
- Order ID + total + damage / delay context
- The reason the refund is warranted (per which policy)
- The requested refund amount
