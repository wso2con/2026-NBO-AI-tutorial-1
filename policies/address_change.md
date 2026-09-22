---
id: address_change
title: Address change
keywords: [address, shipping address, wrong address, change address, redirect, my office]
---

# Changing the shipping address on an order

Address handling is evaluated separately for every open order. A customer's
request to “change my address” does not authorize changing all orders without
identifying which orders are affected and confirming the exact new address.

The automated address-update tool may be used only while the selected order is
`placed` or `preparing`. For these statuses, confirm the complete destination
with the customer before the write. A partial address, a reference such as “my
office,” or an address found only in old conversation memory is not sufficient
for a consequential update.

Once an order reaches `in_transit`, `in_transit_delayed`, `delivered`,
`delivered_damaged`, or any other later state, the order address must not be
changed through the normal update tool. The possible remedy is a carrier
redirect or intercept handled by a human. Escalate a shipment moving toward a
wrong address at priority `high`, and state that the redirect is requested or
pending—not completed. An international shipment already in transit always
requires human escalation even if the carrier appears to support redirects.

For a mixed set of open orders, partition them by status. Eligible unshipped
orders may be updated after customer confirmation; in-transit orders require
separate escalation; delivered orders cannot be redirected under this policy.
Success on one order does not imply that another order was changed.

Never infer that a customer approved cancellation, return, or refund merely
because an address change is no longer possible. Those outcomes are governed
by their own policies and require their own evidence and, where applicable,
confirmation.
