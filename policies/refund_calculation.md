---
id: refund_calculation
title: Refund calculation — how to compute the percentage to refund
keywords: [refund calculation, how to compute refund, refund percentage, cancellation refund, net refund, prior refund, partial refund, refund amount, what percentage to refund]
---

# Refund calculation

How to figure out **what percentage of an order to refund**. The `issue_refund` tool takes a percentage; the backend multiplies it by the order's `total_usd` to get the dollar amount logged in the ledger. The agent's job is to pick the *right* percentage for the situation, and to subtract anything already refunded on that order so the customer isn't paid twice.

## Refund percentages by category

| Refund category                              | Percentage of `total_usd` | Notes                                                                 |
| -------------------------------------------- | ------------------------- | --------------------------------------------------------------------- |
| Damaged on arrival                           | **100%**                  | Photo evidence required — see `damaged_item` policy.                  |
| Cancellation before shipment (placed/preparing) | **90%**               | 10% retained as restocking / handling fee.                            |
| Shipping delay credit (delivery still expected) | **10%**                | Store credit. Customer keeps the order. See `shipping_delay` policy.  |
| Return within window (delivered, undamaged)  | **100%**                  | Within 30 days; see `return_window` policy.                           |
| Goodwill / customer-changed-mind after ship  | **Not eligible**          | Escalate; this is a human-judgment call.                              |

## Net refund formula

When a customer has *already received* a refund on the same order (e.g. a 10% shipping-delay credit was issued earlier, and now they want to cancel), do NOT issue the full new category percentage on top. Compute the **net**:

```
net_refund_percentage = category_percentage − sum(prior refund_percentage on this order)
```

`get_refund_history` returns a `refund_percentage` field on every entry — sum those for entries whose `order_id` matches the order in play. No dollar-math required: the ledger records the fraction of `total_usd` each refund used. (If a legacy entry is ever missing `refund_percentage`, fall back to `amount_usd / order.total_usd` for that entry only.)

### Worked example

Order #1241 — `total_usd = $100`, status `preparing`.
- `get_refund_history` shows one prior refund on #1241 with `refund_percentage = 0.10`, reason `shipping_delay_credit`.
- Customer now wants to cancel.
- Category percentage from the table above: **90%** (cancellation before shipment).
- Net to issue now: `0.90 − 0.10 = 0.80` → server computes `$80`.

Call `issue_refund(order_id="1241", refund_percentage=0.80, reason="cancellation_after_delay_credit")`.

## Required procedure for the agent

1. **`get_order(order_id)`** — confirm `total_usd` and `status`.
2. **`get_refund_history(customer_id)`** — filter entries by `order_id` and SUM their `refund_percentage` values. That is your `already_refunded_percentage`.
3. **`search_policy_kb`** — confirm the category percentage from this policy (don't memorize the table; the numbers may change).
4. **Compute** `net = category_percentage − already_refunded_percentage`. If `net <= 0`, do NOT issue — escalate (customer has already been refunded as much as policy allows).
5. **For a cancellation refund** — call `cancel_order(order_id, reason)` FIRST and confirm it returned `{"ok": True, ...}`. Only then proceed to step 6. Refunding before the cancel succeeds is forbidden (see ordering rule below).
6. **Call `issue_refund(order_id, refund_percentage=net, reason=...)`** with a descriptive reason that references both the category and the prior-refund context (e.g. `"cancel_net_of_prior"`).

## Ordering rule (cancellation refunds)

`cancel_order` MUST succeed before `issue_refund` is called on the same order. Reason: refunding first and then cancelling leaves a window where the customer has been paid back on a still-active order; if the cancel rejects (e.g. the order shipped between calls), the refund has to be reversed by a human. The pre-cancel checks (status, refund history, policy lookup) are all read-only; only `cancel_order` changes state, and only after that succeeds do you issue the refund.

## Anti-patterns

- ❌ Issuing the full category percentage without subtracting prior refunds — the customer is over-paid and audit will flag it.
- ❌ Splitting a single legitimate refund into multiple smaller calls to dodge the cap — explicitly prohibited by `refund_authority`.
- ❌ Picking a percentage from memory without `search_policy_kb` — percentages may have changed since the model's training.
- ❌ Treating a shipping-delay credit as a "refund to original payment" — it's a store credit, customer keeps the order, both can coexist with a later cancel/damage refund (subject to the net formula).

## Related policies

- `damaged_item`, `shipping_delay`, `return_window` — category-specific evidence rules and qualifying conditions.
- `refund_authority` — the agent's cap, the anti-split rule, and over-cap escalation. this should be refered by the point you read refund calulatioj policy. 
- `address_change` — for cancellation requests on already-shipped orders (different path; not eligible for cancellation refund).
