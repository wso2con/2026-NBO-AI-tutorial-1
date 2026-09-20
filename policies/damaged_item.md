---
id: damaged_item
title: Damaged on arrival
keywords: [damaged, broken, cracked, smashed, dented, leaking, defective, arrived broken]
---

# Damaged on arrival

Damaged items require **photo evidence** before a refund or replacement can be
issued. The customer doesn't need to ship the item back — they can keep it.

A return label MUST be sent immediately as a goodwill gesture, even before the
photo arrives. The label costs us nothing; the trust signal is significant.

## Agent action

1. Acknowledge the customer's inconvenience first (empathy before evidence).
2. Send the return label immediately (open a low-priority human ticket if no
   tool is available for direct label issuance).
3. Request a clear photo of the damaged area.
4. **Do NOT issue refund until photo is in hand.**
5. Once photo arrives, issue a full refund with reason `"damaged_item_full_refund"`
   (or `"damaged_item_partial"` depending on the scope of damage).

## Exceptions / escalations

- If the customer can't provide a photo (lost the package, visually impaired, etc.) —
  escalate to a human with priority `normal`.
- Items with **safety implications** (sharp glass, electrical hazards, items for
  children) — escalate regardless of dollar value with priority `high`.
- Repeat damaged delivery to the same address (2+ in 6 months) — escalate with
  priority `high`. This is a fulfillment issue, not a customer issue.
