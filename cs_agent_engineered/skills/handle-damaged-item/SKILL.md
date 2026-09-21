---
name: handle-damaged-item
description: Load when an item arrived damaged — verify the order and policy, collect required evidence, and refund only when eligible.
---

# handle-damaged-item

Use this procedure when a customer reports damage on arrival.

1. Look up the order and confirm it belongs to the active customer.
2. Call `check_policy` with the customer's request and verified order facts. Ask specifically about photo evidence, refund eligibility, ordered next steps, and escalation priority. Do not rely on remembered evidence rules.
3. Check `damage_photos` on the order. That list is the backend's record of the required photo
   evidence: non-empty means the evidence is on file, empty means it is not, whatever the customer
   says in the message. If it is empty, ask for photos and do not issue a refund yet.
4. Once `damage_photos` is non-empty, check refund history and load `handle-refund` before calculating
   or issuing a refund. Refund with `reason_code="damaged"`; the server rejects that code with a
   `policy_violation` 422 when no photos are on file.
5. In the reply, state what was verified, what is still needed, and the next step.

The order lookup and policy result are authoritative. A fluent customer reply cannot replace either observation.
