---
name: handle-cancellation
description: Load before cancelling an order — how to cancel, then figure out the right refund net of anything already credited.
---

# handle-cancellation

Cancellation flips an order's status to `cancelled` AND usually owes the customer money back. The refund is a SEPARATE write — `cancel_order` does not auto-refund — and the right amount depends on policy AND on what's already been refunded on this order. Don't improvise either piece.

**Ordering rule — cancel BEFORE refund, never the other way.** Refunding first and then cancelling leaves a window where you've paid the customer back on a still-active order; if the cancel then rejects (e.g. the warehouse shipped it between your calls), you have to reverse the refund and explain the mess. The pre-cancel checks (status, refund history, policy) are all read-only; only `cancel_order` flips state, and only after that succeeds do you call `issue_refund`.

## High-level flow

1. **Look up the order** — `get_order(order_id)`. Note `total_usd` and `status`. On `ownership_mismatch` (code 403), do NOT retry; the order isn't theirs.
2. **Check status.** If the order has already shipped (any status beyond `placed` / `preparing`), cancellation is not allowed. Consult `address_change` policy for the carrier-redirect path; for delivered orders consult `return_window` policy and treat as a return, not a cancel. Stop here.
3. **Check refund history** — `get_refund_history(customer_id)`. Filter entries by THIS `order_id` and SUM their `refund_percentage` values — that's `already_refunded_pct` (a fraction). No dollar-math: the ledger records the fraction each refund used.
4. **Look up the refund-calculation policy** — `list_policies`, then `get_policy("refund_calculation")`. Read the current cancellation percentage; do not copy a value from this procedure.
5. **Compute net refund percentage:**

       net_pct = cancellation_pct − already_refunded_pct

   If `net_pct <= 0`, do NOT call `issue_refund` — the customer has already been refunded everything policy allows. Cancel only, then explain in the reply.
6. **Cancel** — `cancel_order(order_id, reason)` with a specific reason (audit-logged). On success you'll get a cancel ref.
7. **Refund the net** — `issue_refund(order_id, refund_percentage=net_pct, reason="cancel_net_of_prior")`. The server multiplies by `total_usd` and logs both pct and dollar amount. If the call returns a `policy_violation` 403 (over cap), DO NOT split — escalate the full case.
8. **Confirm in the reply** — cancel ref, refund ref + dollar amount, what's deducted (prior credit) and why, payment-reversal timing.

## Anti-patterns

- ❌ Calling `cancel_order` before checking status — wastes a turn on `order_already_shipped`.
- ❌ Issuing the full cancellation percentage without subtracting prior refunds — over-pays the customer; audit will flag it.
- ❌ Skipping the refund-history check because the customer didn't mention a prior credit — they often forget; the ledger is the source of truth.
- ❌ Picking a percentage from memory instead of `refund_calculation` policy — percentages change; the policy is authoritative.
- ❌ **Calling `issue_refund` before `cancel_order` succeeds** — wrong order. If the cancel then rejects, you've paid out on a live order. Cancel first, refund second.
- ❌ Cancelling and then forgetting to refund — the customer never gets their money. The `cancel_order` response carries a `next_step` reminder; act on it.
- ❌ Calling `issue_refund` twice to split a single net refund — that's a `refund_authority` violation.
- ❌ Treating an address-fix request as a cancel — redirect to address change instead; it's faster.

## Related

- `cancel_order(order_id, reason)` — flips status, no refund.
- `issue_refund(order_id, refund_percentage, reason)` — the refund write. See `handle-refund` for the broader refund flow.
- `refund_calculation` policy — percentages by category, the net-refund formula.
- `refund_authority` policy — the cap and the anti-split rule.
- `address_change` policy — the carrier-redirect path for shipped orders.
- `return_window` policy — delivered orders the customer wants to "cancel".
