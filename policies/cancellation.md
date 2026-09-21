---
id: cancellation
title: Cancelling an order before it ships
keywords: [cancel, cancellation, cancel my order, call it off, stop the order, don't want it anymore, taking too long, scrap the order]
---

# Cancelling an order before it ships

## Scope

This policy governs a customer's request to stop an order they have already
placed and not yet received. It is the controlling category policy whenever the
customer asks to cancel, regardless of the reason they give for asking.

A cancellation request is frequently accompanied by a complaint about delay.
The complaint does not convert the request into a `shipping_delay` case.
`shipping_delay` governs compensation for an order the customer still expects
to receive; it deliberately does not authorize cancellation, and its silence is
not a refusal. When the customer has asked to stop the order, apply this policy
and treat any delay as context, not as the request. A customer does not need a
qualifying delay, or any particular reason, to cancel an unshipped order.

## Eligibility

Cancellation through the automated tool is available only while the order is
`placed` or `preparing`. Eligibility is determined by the order's recorded
status, not by whether the estimated delivery date has passed. An order that is
overdue but still `preparing` has not shipped and remains cancellable.

Once the order reaches `in_transit`, `in_transit_delayed`, `delivered`,
`delivered_damaged`, or any later state, it must not be cancelled through this
tool. A shipment already moving requires a carrier intercept handled by a
human; escalate at priority `normal` and state that the intercept is requested,
not completed. A delivered order the customer wants to undo is a return and is
governed by `return_window`; a delivered order that arrived broken is governed
by `refund_damaged_item`.

## Resolution sequence

1. Confirm the exact order the customer means. Never cancel a different order
   because it is easier to cancel or closer to the complaint they described.
2. Read the order's current status and confirm it is `placed` or `preparing`.
3. Retrieve refund history for that order so prior credits are known before
   any money moves.
4. Cancel the order. Cancellation is the state change; it does not return any
   money on its own.
5. Only after cancellation succeeds, issue the refund calculated under
   `refund_calculation` using reason code `cancellation`. The cancellation
   entitlement is net of every prior refund percentage recorded against the
   same order, including a shipping-delay credit. If the net entitlement is
   zero or negative, complete the cancellation and issue nothing.
6. Confirm both the cancellation and the refund amount to the customer, and
   name any prior credit that was deducted.

Refunding before the cancellation succeeds is prohibited: it pays out on a
live order. If the cancellation is rejected because the order shipped between
the checks and the write, do not refund; move to the intercept path above.

## Authority and escalation

The calculated refund remains subject to `refund_authority`. An over-cap
cancellation refund is escalated in full and never split, reduced to fit, or
recoded. Escalate at priority `normal` when the order cannot be cancelled and
the customer still wants it stopped, and at priority `high` when records are
inconsistent or the request appears adversarial. An escalation is a review
request; it does not cancel the order and must not be described as though the
order were cancelled.
