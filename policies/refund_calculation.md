---
id: refund_calculation
title: How to calculate the refund percentage to issue
keywords: [refund calculation, how to compute refund, refund percentage, cancellation refund, net refund, prior refund, partial refund, refund amount, what percentage to refund]
---

# How to calculate the refund percentage to issue

Refund entitlement is expressed as a percentage of the selected order's
`total_usd`. This policy supplies calculation rules only; it does not establish
that the customer meets the category-specific evidence or timing conditions.
The category policy must be applied first.

| Category | Entitlement | Conditions |
| --- | ---: | --- |
| Damaged on arrival | 100% | Required evidence under `refund_damaged_item` |
| Cancellation before shipment | 90% | Order is `placed` or `preparing` |
| Shipping delay | 10% store credit | Delivery is more than 3 business days late |
| Return | 100% | Eligible under `return_window` |
| Changed mind after shipment | None | Human review required |

The percentages are maximum category entitlements, not automatic awards. For
partial damage, use the percentage of the order represented by the affected
item or items. Do not apply the full-order percentage merely because one item
is unusable. A shipping-delay credit does not convert into a cancellation or
return refund and does not close the order.

## Net entitlement

Before issuing any refund or credit, retrieve refund history and subtract all
prior refund percentages recorded for the same order from the applicable
category percentage:

```
net_refund_percentage = category_percentage − sum(prior refund_percentage on this order)
```

- If the net percentage is zero or negative, no additional refund is owed. Do
  not create a zero-value transaction and do not move the claim to a different
  reason code to manufacture entitlement.
- A cancellation refund is allowed only after `cancel_order` succeeds. Checking
  that an order appears cancellable is not equivalent to cancellation.
- Shipping-delay compensation is store credit and leaves the order active.
- A return refund becomes eligible only after the customer confirms the item
  has been shipped back, as required by `return_window`.
- Every calculated transaction remains subject to `refund_authority`.

## Worked interpretation rules

If a $100 order qualifies for a 90% cancellation refund and already received a
10% shipping-delay credit recorded as `refund_percentage=0.10`, the remaining
cancellation entitlement is 80%, or $80. If the same order had already
received 90% or more, nothing further is owed under the cancellation category.

Use the percentage values stored in refund history, not a dollar amount divided
back into a percentage unless no percentage is available. Never subtract a
refund from a different order, even when the items or totals are similar.
