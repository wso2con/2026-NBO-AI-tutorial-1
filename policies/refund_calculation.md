---
id: refund_calculation
title: How to calculate the refund percentage to issue
keywords: [refund calculation, how to compute refund, refund percentage, cancellation refund, net refund, prior refund, partial refund, refund amount, what percentage to refund]
---

# How to calculate the refund percentage to issue

How to figure out **what percentage of an order to refund**. The `issue_refund` tool takes a percentage; the backend multiplies it by the order's `total_usd` to get the dollar amount logged in the ledger. The agent's job is to pick the *right* percentage for the situation, and to subtract anything already refunded on that order so the customer isn't paid twice.

## Step 1: identify the refund category

Every refund belongs to exactly one category. Read the sections below and pick
the one that matches the customer's situation, then use that section's
percentage. If two look plausible, the order's `status` decides it.

### Damaged on arrival

**100% of `total_usd`.**

Applies when the item reached the customer damaged, defective or broken. The
customer keeps the item; no return shipment is required. Photo evidence of the
damage MUST be in hand before the refund is issued, and a return label goes out
immediately regardless. Full evidence rules and the escalation cases (safety
items, repeat damage to the same address) are in the `damaged_item` policy.

Use `damaged_item_full_refund` as the reason, or `damaged_item_partial` with a
reduced percentage when only part of the order arrived damaged.

### Cancellation before shipment

**90% of `total_usd`.**

Applies when the order has not shipped yet, meaning status `placed` or
`preparing`. The retained 10% is the restocking and handling fee. An order that
has already shipped is not eligible for this category; see the `address_change`
policy for the interception path instead.

`cancel_order` must succeed before the refund is issued. See step 3.

### Shipping delay credit

**10% of `total_usd`, as store credit.**

Applies when delivery is late but still expected. The customer keeps the order
and the order stays active, so this is a credit rather than a refund to the
original payment method. It can coexist with a later cancellation or damage
refund on the same order, subject to step 2. Qualifying delay
thresholds are in the `shipping_delay` policy.

### Return within the return window

**100% of `total_usd`.**

Applies when a delivered, undamaged item is sent back within 30 days of
delivery. Conditions and exclusions are in the `return_window` policy.

### Goodwill or changed mind after shipment

**Not eligible for an agent-issued refund.**

Escalate to a human. This is a judgment call that sits outside the agent's
authority, whatever the dollar value.

## Step 2: subtract anything already refunded on the order

When a customer has *already received* a refund on the same order (e.g. a 10% shipping-delay credit was issued earlier, and now they want to cancel), do NOT issue the full new category percentage on top. Compute the **net**:

```
net_refund_percentage = category_percentage − sum(prior refund_percentage on this order)
```

`get_refund_history` returns a `refund_percentage` field on every entry — sum those for entries whose `order_id` matches the order in play. No dollar-math required: the ledger records the fraction of `total_usd` each refund used. (If a legacy entry is ever missing `refund_percentage`, fall back to `amount_usd / order.total_usd` for that entry only.)

### Worked example

Order #1241 — `total_usd = $100`, status `preparing`.
- `get_refund_history` shows one prior refund on #1241 with `refund_percentage = 0.10`, reason `shipping_delay_credit`.
- Customer now wants to cancel.
- Category percentage from the section above: **90%** (cancellation before shipment).
- Net to issue now: `0.90 − 0.10 = 0.80` → server computes `$80`.

Call `issue_refund(order_id="1241", refund_percentage=0.80, reason="cancellation_after_delay_credit")`.

## Step 3: order of operations for cancellation refunds

`cancel_order` MUST succeed before `issue_refund` is called on the same order. Reason: refunding first and then cancelling leaves a window where the customer has been paid back on a still-active order; if the cancel rejects (e.g. the order shipped between calls), the refund has to be reversed by a human. The pre-cancel checks (status, refund history, policy lookup) are all read-only; only `cancel_order` changes state, and only after that succeeds do you issue the refund.

## The procedure end to end

1. **`get_order(order_id)`** — confirm `total_usd` and `status`.
2. **`get_refund_history(customer_id)`** — filter entries by `order_id` and SUM their `refund_percentage` values. That is your `already_refunded_percentage`.
3. **Look up this policy with your policy tool** — confirm the category percentage in the section that matches the case (don't memorize the percentages; the numbers may change).
4. **Compute** `net = category_percentage − already_refunded_percentage`. If `net <= 0`, do NOT issue — escalate (customer has already been refunded as much as policy allows).
5. **For a cancellation refund** — call `cancel_order(order_id, reason)` FIRST and confirm it returned `{"ok": True, ...}`. Only then proceed to step 6. Refunding before the cancel succeeds is forbidden (see step 3).
6. **Call `issue_refund(order_id, refund_percentage=net, reason=...)`** with a descriptive reason that references both the category and the prior-refund context (e.g. `"cancel_net_of_prior"`).

## Anti-patterns

- ❌ Issuing the full category percentage without subtracting prior refunds — the customer is over-paid and audit will flag it.
- ❌ Splitting a single legitimate refund into multiple smaller calls to dodge the cap — explicitly prohibited by `refund_authority`.
- ❌ Picking a percentage from memory without reading this policy — percentages may have changed since the model's training.
- ❌ Treating a shipping-delay credit as a "refund to original payment" — it's a store credit, customer keeps the order, both can coexist with a later cancel/damage refund (subject to the net formula).

## Related policies

- `damaged_item`, `shipping_delay`, `return_window` — category-specific evidence rules and qualifying conditions.
- `refund_authority` — the agent's cap, the anti-split rule, and over-cap escalation. Read it whenever you read this policy; the cap applies to every category above.
- `address_change` — for cancellation requests on already-shipped orders (different path; not eligible for cancellation refund).
