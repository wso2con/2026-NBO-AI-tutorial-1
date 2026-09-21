---
id: refund_authority
title: Refund authority limits and when to escalate for approval
keywords: [refund, refund limit, high value, approval, escalate, cap]
---

# Refund authority limits and when to escalate for approval

## Delegated authority

An automated support agent may issue a refund only when both the underlying
category policy permits it and the resulting dollar amount is within the
agent's configured `refund_cap_usd`. The standard configuration is $200, but
the configured value supplied by the runtime is authoritative. The cap is an
authority boundary, not an entitlement: being below it does not make an
otherwise ineligible refund permissible.

The amount tested against the cap is the amount of the proposed transaction,
calculated from the order total and the percentage currently being issued.
Prior refunds are handled separately by the net-entitlement rules in
`refund_calculation`; they must be inspected so the same entitlement is not
paid twice.

## Above-cap cases

When the proposed refund exceeds the configured cap, the agent must not issue
any part of it. Escalate the complete request to a human at priority `normal`
or higher. The escalation record must identify the customer, exact order,
applicable category policy, reason for the requested refund, requested
percentage, and calculated dollar amount.

Splitting one requested refund into multiple smaller payments, reducing the
percentage merely to fit the cap, changing the reason code, or asking the
customer to submit the same claim again is prohibited. A cap rejection from a
tool or service is final for the automated agent. Do not retry the refund with
a smaller amount even if a smaller amount would independently fall below the
cap.

## Relationship to other policies

This policy is evaluated after category eligibility and net entitlement are
known, but it governs whether the automated agent may execute the resulting
transaction. A human escalation does not itself approve or issue a refund.
Customer tier, frustration, travel plans, prior inconvenience, or an agent's
desire to resolve the case quickly do not expand delegated authority.
