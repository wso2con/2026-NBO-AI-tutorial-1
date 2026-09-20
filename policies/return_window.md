---
id: return_window
title: Return window
keywords: [return, return window, send back, 30 days, unwanted, changed mind]
---

# Return window

Customers may return undamaged items within **30 days of delivery** for a full
refund. Items damaged in transit are covered separately under the
`damaged_item` policy (no 30-day limit for damage claims, but reasonable
timeliness applies — see escalation rule below).

## Eligibility check

- Item is **undamaged** and **unused** (or in resalable condition).
- Delivery date is **within 30 days** of the return request.
- Customer is verified.

## Agent action

1. Verify delivery date — look up the order, check the `placed` date and assume
   delivery happens ~3 days after.
2. If within window: send return label (escalate with priority `low` since we
   don't have a direct label tool), issue refund once the customer confirms
   they've shipped it back.
3. Cite this policy by name in the reply ("Per our 30-day return window...").

## Exceptions

- Past the 30-day window: politely decline and explain. Offer a small store
  credit ($5) as goodwill if the customer is a long-tenured account (`joined`
  more than 12 months ago).
- Items past 30 days but damaged: route through `damaged_item` policy instead.
- Customer claims they tried to return earlier — escalate with priority
  `normal` and let a human verify.
