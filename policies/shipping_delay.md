---
id: shipping_delay
title: Shipping delay
keywords: [delay, delayed, late, shipping, cancellation, refund, tracking, where is my order]
---

# Compensation for late or delayed deliveries

This policy applies to an order that remains active and is expected to reach
the customer after its estimated delivery date. It provides store credit for
qualifying delay; it does not cancel the shipment, create a return, or convert
the order into a lost-package claim.

Measure delay using the operational order record's business-day delay value.
Do not calculate delay from conversational dates when the system already
provides `delivery_days_late`.

- More than 3 and no more than 7 business days late qualifies for store credit
  equal to 10% of the order total.
- More than 7 business days late qualifies for store credit equal to 25% of
  the order total.
- Three business days late or less does not qualify. “More than 3” begins at
  four business days; “more than 7” begins at eight.

Use reason code `shipping_delay_credit`. The order remains active and the
customer still receives it. Do not promise cancellation, intercept, replacement,
or a full refund merely because delay credit is available.

Delay caused by an incorrect customer-supplied address or a missed carrier
pickup does not qualify. When the cause is unknown, verify it if a tool exposes
the cause; otherwise do not invent an exclusion. Credits already recorded for
the same order count toward net entitlement under `refund_calculation`, and the
resulting transaction remains subject to `refund_authority`.

If the customer's actual goal is receipt before a deadline, explain the known
tracking state and treat compensation as a secondary remedy. A credit does not
prove the shipment will arrive by the customer's deadline.
