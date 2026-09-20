---
id: address_change
title: Address change
keywords: [address, shipping address, wrong address, change address, redirect, my office]
---

# Address change

Shipping addresses may be updated **only** on orders that have not yet shipped
(status: `placed` or `preparing`). After shipment, the carrier owns the package
and the customer must use the carrier's redirect service directly.

## Agent action

1. Look up the order — check the `status` field.
2. **If status is `placed` or `preparing`:** update the shipping address.
   Confirm the new address in the reply.
3. **If status is anything else** (`in_transit_delayed`, `in_transit`,
   `delivered`, `delivered_damaged`): do NOT attempt the update — the
   backend will reject with `order_already_shipped` and a `remediation` of
   `redirect_to_carrier`. Politely explain the constraint and direct the
   customer to the carrier's redirect service (typically a link on the
   tracking page).

## Anti-pattern

Attempting an address update blindly without first checking status. The
backend will reject, you'll waste a turn, and the customer sees you fumble.
Always look up the order first.

## Exceptions

- Customer claims package is being delivered to wrong address by mistake
  (their old address from a previous order): escalate with priority `high` —
  this may need carrier intercept.
- Address update for international shipping in transit: escalate to human;
  carrier rules vary by region.
