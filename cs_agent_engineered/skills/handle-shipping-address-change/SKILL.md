---
name: handle-shipping-address-change
description: Load if user mentioned changing the shipping address - This explain show to proceed in such occasions.
---

# handle-address-change

Shipping addresses live per-order; there is no account-level address. When the customer says "my address has changed," do NOT silently update every order — and do NOT update only the one they happened to mention while leaving the rest stale. Surface the full picture and let the customer decide.

## High-level flow

1. **List the customer's recent orders** — `get_customer_orders(limit=N)`. You need the full picture before touching anything.
2. **Partition orders by status:**
   - `placed` / `preparing` → updatable.
   - `in_transit` / `in_transit_delayed` → shipped; address can't be changed. Carrier-intercept territory.
   - `delivered` / `delivered_damaged` / `cancelled` → moot; the address no longer matters.
3. **Confirm the new address** with the customer if it's ambiguous or partial — don't guess country / postcode from one line.
4. **Reply with the picture and ASK before acting.** In ONE message:
   - Name the order they mentioned, and what you'd do for it (update vs. carrier-intercept, depending on status).
   - List the OTHER updatable orders by id + current address, and ask "should I update these too?" — do not assume yes.
   - List the in-transit orders and ask whether they want a carrier intercept requested on any of them (intercepts cost time and aren't guaranteed; the customer chooses).
   - Skip delivered / cancelled orders.
5. **Wait for the customer's reply.** Act only on what they explicitly confirmed.
6. **Execute the confirmed list:**
   - For each updatable order they said yes to → `update_shipping_address(order_id, new_address)`. Collect the audit refs.
   - For each intercept they confirmed → `escalate_to_human` with priority `high`, reason "carrier intercept for order=<id>, new_address=<addr>". Cite `address_change` policy.
7. **Final reply** — what was updated (orders + refs), which intercepts were escalated, anything left untouched at the customer's request.

## Anti-patterns

- ❌ Auto-updating every open order without asking — the customer didn't authorise a fan-out.
- ❌ Updating only the order the customer mentioned and ignoring that other open orders still ship to the old address — that's exactly the bug they're trying to prevent.
- ❌ Blanket-escalating every shipped order for carrier intercept — ask first; they may be fine letting a near-delivery package land.
- ❌ Cancelling shipped orders as a workaround for the wrong address — that's a separate decision the customer hasn't asked for. Confirm intent before cancelling.
- ❌ Promising the carrier WILL redirect — you're requesting an intercept, not guaranteeing one.

## Related

- `get_customer_orders(limit)` — the fan-out source.
- `update_shipping_address(order_id, new_address)` — the write.
- `escalate_to_human` — the carrier-intercept path for shipped orders.
- `address_change` policy — status rules and the carrier-redirect language.
- `handle-cancellation` — only if the customer actually wants to abandon, not redirect.
