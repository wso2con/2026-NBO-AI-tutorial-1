---
id: refund_damaged_item
title: Damaged items
keywords: [damaged, broken, cracked, smashed, dented, leaking, defective, arrived broken]
---

# Refunds and replacements for items that arrive damaged

## Scope and evidence standard

This policy applies when merchandise is alleged to have arrived broken,
cracked, smashed, dented, leaking, torn, or otherwise unusable because of
physical damage. Cosmetic preference, ordinary wear, and a customer changing
their mind are not damage claims and are considered under `return_window`.
Where an order is both within the return window and damaged, this policy takes
precedence.

A refund or replacement requires clear photo evidence attached to the order in
`damage_photos`. A customer's statement that a photo exists, an offer to send a
photo later, or a filename included in chat is not evidence on file. The order
record is authoritative. The image should show the affected item and enough of
the damage to support the claim; agents do not need to assess photographic
quality when the order record already marks the evidence as accepted.

## Resolution sequence

1. Confirm the exact order and damaged item. Do not use evidence belonging to a
   different order or another item in the same customer's history.
2. Check whether accepted photo evidence is on the selected order.
3. If no photo is on file, do not issue compensation. Ask the customer to
   upload the photo through the normal support channel and open a missing-photo
   exception by escalating at priority `normal`. The ticket is a review
   request; it is not approval and does not mean a refund has been issued.
4. If accepted evidence is on file, determine the affected share of the order.
   Damage to the entire order qualifies for the damaged-on-arrival percentage
   in `refund_calculation`. Partial damage may qualify only for a proportional
   percentage representing the affected part of the order.
5. Apply `refund_authority` and subtract prior refunds as required by
   `refund_calculation` before issuing anything.

## Mandatory escalation

Escalate at priority `high` when the damage creates a safety hazard, when the
customer reports two or more damaged deliveries to the same address within six
months, or when the available records appear inconsistent. These escalations
do not waive the photo requirement. An agent must not describe an escalation,
requested upload, or pending review as a completed refund or replacement.
