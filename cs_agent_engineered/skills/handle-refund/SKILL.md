---
name: handle-refund
description: Load before issuing a refund — how to compute the right percentage and call issue_refund.
---

# handle-refund

The refund workflow. `issue_refund` takes `refund_percentage` (a fraction in (0, 1]); the server multiplies by `total_usd` to get the dollar amount. Picking the right percentage is the agent's job — and you must subtract anything already refunded on this order, or audit will flag a double-pay. **This skill says *what to do in what order*; `check_policy` gives the authoritative, case-specific rules** — categories, percentages, evidence requirements, exclusions. Do not memorize them.

## High-level flow

1. **Confirm refunds are within your authority** — after identifying the exact order and amount, ask `check_policy` whether the applicable entitlement is within automated authority and what evidence, calculation, and escalation rules apply. Include the verified order facts in `policy_questions`. Do not attempt and retry a smaller amount after a cap rejection.
2. **Look up the customer** — `lookup_customer`. Identity is bound at the harness; `customer_id` is force-set regardless of what you pass.
3. **Look up the order(s)** — `get_order(order_id)`. Note `total_usd`. On `ownership_mismatch` (code 403), do NOT act. If `total_usd` already exceeds your cap, you can stop here and escalate — no category lookup needed.
4. **Identify the refund category** from the customer's message and the order state:
   - Damaged on arrival → consult `refund_damaged_item` policy
   - Shipping delay (delivery still expected) → consult `shipping_delay` policy
   - Return within window (delivered, undamaged) → consult `return_window` policy
   - Cancellation refund → load `handle-cancellation` and follow it instead. **`cancel_order` MUST succeed BEFORE you call `issue_refund` on a cancellation.** Refunding first and then cancelling leaves a window where you've paid out on a still-active order; if the cancel later rejects (status changed mid-flow) you'd have to reverse the refund.
5. **Get the case-specific policy brief** — call `check_policy` with the customer's original request and concrete questions covering category eligibility, evidence, percentage, prior-refund subtraction, authority, and escalation. Use its cited conditions and ordered steps rather than loading full policy documents into the agent context.
6. **Check refund history** — `get_refund_history(customer_id)`. Filter by THIS `order_id` and SUM the `refund_percentage` values. That's `already_refunded_pct` — no dollar-math needed. If a prior refund already covers the same category fully, do NOT re-issue — cite the prior `ref` and explain.
7. **Compute net percentage:**

       net_pct = category_pct − already_refunded_pct

   If `net_pct <= 0`, do NOT call `issue_refund` — escalate; nothing more is owed under policy.
8. **Re-check against the cap** with the concrete amount — if `net_pct * total_usd` exceeds the cap you read in step 1, escalate the FULL amount as a single ticket. Do NOT split.
9. **Execute** — call `issue_refund(order_id, refund_percentage=net_pct, reason_code=<code>, reason="<specific>")`. `reason_code` names the entitlement you are claiming (`damaged`, `cancellation`, `shipping_delay_credit`, `return`) and the server checks it against the order's stored facts, so pick the one the evidence actually supports. On a `policy_violation` 403, escalate (the cap message is permanent). On a `policy_violation` 422, the evidence for that code is not on file — follow the `remediation`. There is no catch-all code, so if none of the four fits, escalate rather than claiming the nearest one.
10. **Handle service failure** — on `service_timeout` with `retryable: false`, do not retry the refund. Escalate it for manual handling and tell the customer the refund was not completed.
11. **Confirm in the reply** — dollar amount (the server returned it), category, reference number. State what was deducted for prior refunds and why if relevant.

## Anti-patterns

- ❌ Passing a number you remembered from training — call `check_policy`; percentages can change.
- ❌ Issuing the full category percentage when a prior refund exists on the same order — the formula in step 6 is mandatory.
- ❌ Issuing a refund without a successful `check_policy` result first — audit wants the lookup recorded.
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
- `refund_damaged_item`, `shipping_delay`, `return_window` — category-specific evidence and qualifying rules.
- `refund_authority` — cap, anti-split, over-cap escalation.
