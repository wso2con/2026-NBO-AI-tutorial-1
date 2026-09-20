# Customer memory — cust_002 (Bob Martinez)

Episodic memory holds **pointers and observations**, not data. Verify every
pointer with a tool call before acting on it — the audit ledger and orders
API are the source of truth; this file is just notes from past interactions.

## Open promises (verify before replying)

- **2026-05-01** — Promised Bob a replacement for order #2210 by ~2026-05-06.
  Tracked via TICKET-1001 (warehouse / batch-QC investigation). Look it up
  via `get_open_tickets(cust_002)` before responding to anything about #2210.

## Observations

- **2026-05-01** — SECOND damaged delivery to Lindenstrasse, Munich within
  6 months. Pattern is fulfillment-side, not customer-side. Don't gaslight
  Bob as a one-off.
- Tone on the prior call: understanding but pointed. He is keeping score.

## How to handle Bob

- Call `get_refund_history(cust_002)` BEFORE issuing any refund on #2210 —
  we've already paid out on this order; do **not** double-refund.
- Call `get_open_tickets(cust_002)` to confirm TICKET-1001's status. If
  it's still `open` with no warehouse progress, escalate priority `high`
  and reference TICKET-1001 in the new ticket's reason.
- Customer tier / tenure / contact info → `lookup_customer`, not this file.
