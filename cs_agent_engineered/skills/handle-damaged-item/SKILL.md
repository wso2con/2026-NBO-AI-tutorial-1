---
name: handle-damaged-item
description: Load when an item arrived damaged — verify the order and policy, collect required evidence, arrange the return, and refund only when eligible.
---

# handle-damaged-item

Use this procedure when a customer reports damage on arrival.

1. Look up the order and confirm it belongs to the active customer.
2. Read the `damaged_item` policy. Do not rely on remembered evidence rules.
3. If the customer has not supplied the required photo evidence, ask for it and do not issue a refund yet.
4. Arrange the return-label step through `escalate_to_human` with a specific reason that records the damaged-item return.
5. After the required evidence is available, check refund history and load `handle-refund` before calculating or issuing a refund.
6. In the reply, state what was verified, what is still needed, and the next step.

The order lookup and policy result are authoritative. A fluent customer reply cannot replace either observation.
