---
name: handle-refund
description: Load before issuing a refund — how to compute the right percentage and call issue_refund.
---

# handle-refund

The refund workflow. `issue_refund` takes `refund_percentage` (a fraction in (0, 1]); the server multiplies by `total_usd` to get the dollar amount. Picking the right percentage is the agent's job — and you must subtract anything already refunded on this order, or audit will flag a double-pay. **This skill says *what to do in what order*; the policies (via `search_policy_kb`) give the *authoritative rules*** — categories, percentages, evidence requirements, exclusions. Do not memorize them.

## High-level flow

1. **Confirm refunds are within your authority** — `search_policy_kb` for `refund_authority` FIRST. It tells you your dollar cap, the anti-split rule, and that over-cap refunds must be escalated as a single ticket (do NOT attempt and retry smaller). Knowing the cap up front means you can short-circuit obvious over-cap requests straight to escalation without spending tool calls on category / history math you won't use.
2. **Look up the customer** — `lookup_customer`. Identity is bound at the harness; `customer_id` is force-set regardless of what you pass.
3. **Look up the order(s)** — `get_order(order_id)`. Note `total_usd`. On `ownership_mismatch` (code 403), do NOT act. If `total_usd` already exceeds your cap, you can stop here and escalate — no category lookup needed.
4. **Identify the refund category** from the customer's message and the order state:
   - Damaged on arrival → consult `damaged_item` policy
   - Shipping delay (delivery still expected) → consult `shipping_delay` policy
   - Return within window (delivered, undamaged) → consult `return_window` policy
   - Cancellation refund → load `handle-cancellation` and follow it instead. **`cancel_order` MUST succeed BEFORE you call `issue_refund` on a cancellation.** Refunding first and then cancelling leaves a window where you've paid out on a still-active order; if the cancel later rejects (status changed mid-flow) you'd have to reverse the refund.
5. **Search the policies you need** — `search_policy_kb` for the category policy AND for `refund_calculation` (it has the percentage table and the net-refund formula). Evidence rules and exclusions live in the category policy; percentages live in `refund_calculation`. Read both.
6. **Check refund history** — `get_refund_history(customer_id)`. Filter by THIS `order_id` and SUM the `refund_percentage` values. That's `already_refunded_pct` — no dollar-math needed. If a prior refund already covers the same category fully, do NOT re-issue — cite the prior `ref` and explain.
7. **Compute net percentage:**

       net_pct = category_pct − already_refunded_pct

   If `net_pct <= 0`, do NOT call `issue_refund` — escalate; nothing more is owed under policy.
8. **Re-check against the cap** with the concrete amount — if `net_pct * total_usd` exceeds the cap you read in step 1, escalate the FULL amount as a single ticket. Do NOT split.
9. **Execute** — `issue_refund(order_id, refund_percentage=net_pct, reason="<specific>")`. On a `policy_violation` 403, escalate (the cap message is permanent).
10. **Confirm in the reply** — dollar amount (the server returned it), category, reference number. State what was deducted for prior refunds and why if relevant.

## Anti-patterns

- ❌ Passing a number you remembered from training — call `search_policy_kb`; percentages can change.
- ❌ Issuing the full category percentage when a prior refund exists on the same order — the formula in step 6 is mandatory.
- ❌ Issuing a refund without consulting the relevant category policy first — audit wants the lookup recorded.
- ❌ Re-issuing a refund on a follow-up — `get_refund_history` is the source of truth; episodic memory can be stale.
- ❌ Splitting an over-cap refund into multiple smaller calls — explicit `refund_authority` violation.
- ❌ Promising timelines for things outside your control (replacement shipments, carrier reschedules).

## Communication

- Empathetic acknowledgement when warranted — one short phrase, then resolution.
- Cite the policy by name in the reply, in plain language.
- State specific numbers and reference IDs — never vague amounts.

## When this skill doesn't fit

Novel cases — subscription proration, refund to a different payment method, gift card refund, multi-order combined refund, fraud signals, legal threats — **escalate via `handle-escalation`**.

## Related policies (consulted by this skill)

- `refund_calculation` — percentages by category and the net-refund formula.
- `damaged_item`, `shipping_delay`, `return_window` — category-specific evidence and qualifying rules.
- `refund_authority` — cap, anti-split, over-cap escalation.
