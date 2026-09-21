"""CustomerSupportClient — file-backed mock backend, isolated per agent.

Each agent gets its own data directory under `mocks/data/<agent_id>/`.
first-cut and engineered never share runtime state — one agent's refunds, cancellations,
or address updates are completely invisible to the other. This is what
makes the side-by-side demo a real comparison: each agent acts on the
world it created, not a world contaminated by the other agent's writes.

The canonical seed lives at `mocks/seeds/*.json` and is read-only and
shared: both agents start from the same fixtures. On first instantiation
(or after `reset()`), the agent's own data files are repopulated from
those shared seeds.

Why file-backed and not pure in-memory: engineered's MCP subprocesses are
re-spawned whenever the cached Agent is dropped (e.g. on /api/end_session
during the §5 episodic-memory demo). An in-memory client would lose
every mutation on that respawn — refunds would silently disappear,
ledger entries would vanish, and the demo would run backwards from
intended. Disk-backed state survives the subprocess lifecycle because
the new client just reads the JSON it was written to.

The agents do hold a per-process in-memory cache for speed (every read
hitting the disk would parse the same JSON N times per turn). The cache
is refreshed from disk on every mutation made by THIS instance.

To reset state: call `reset()` (or `reset_data_files(agent_id)` without
an instance) — wipes only that agent's data files and reseeds them from
the shared canonical seeds. first-cut's /api/reset and engineered's /api/reset each
reset their own agent's data; the web UI's "Reset" button hits both
endpoints so the lab returns to a known starting state across the board.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from demo_clock import now_iso
from mocks.models import Customer, Order

SEEDS_DIR = Path(__file__).parent / "seeds"
DATA_ROOT = Path(__file__).parent / "data"

CUSTOMERS_FILE = "customers.json"
ORDERS_FILE = "orders.json"
LEDGER_FILE = "ledger.json"
FAULTS_FILE = "faults.json"


def _load_seed(name: str):
    return json.loads((SEEDS_DIR / name).read_text())


def _agent_data_dir(agent_id: str) -> Path:
    if not agent_id:
        raise ValueError("agent_id is required — each agent has its own data dir")
    return DATA_ROOT / agent_id


def _write_json(path: Path, value) -> None:
    """Publish `value` at `path` in one step.

    The MCP servers run as separate processes against these same files, so a
    reader can land at any instant during a write. `Path.write_text` truncates
    first and fills after, which gives that reader a window on an empty or
    half-written file — and `unlink`-then-rewrite gives it a window on no file
    at all, which is the `[Errno 2] No such file or directory: orders.json` a
    tool call hits when it runs while a reset is in flight.

    Writing a temp file in the same directory and `os.replace`-ing it over the
    target closes both windows: the rename is atomic on POSIX, so every reader
    sees either the whole previous file or the whole new one, and the target
    never stops existing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2) + "\n"
    # Same directory as the target: os.replace is only atomic within a
    # filesystem, and /tmp may well be a different one.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _seed_if_missing(data_dir: Path) -> None:
    """Lazy initialisation: copy shared seeds into the agent's data dir for
    any file that doesn't exist yet. Doesn't touch files that ARE already
    present — that's how we preserve mid-demo state across MCP subprocess
    restarts."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in (CUSTOMERS_FILE, ORDERS_FILE, LEDGER_FILE):
        target = data_dir / name
        if target.exists():
            continue
        _write_json(target, _load_seed(name))


def reset_data_files(agent_id: str) -> None:
    """Restore the named agent's data files to the shared seed state.
    Used by /api/reset on both agents (first-cut calls it via `_client.reset()`;
    engineered calls it directly from main.py so the next-spawned MCP subprocess
    re-reads clean state). Only the named agent's data is touched — the
    sibling agent's state is left alone."""
    data_dir = _agent_data_dir(agent_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Overwrite in place rather than unlink-then-reseed. The MCP subprocess may
    # be mid-tool-call against these files, and a file that briefly does not
    # exist fails that call outright; a file replaced atomically just serves it
    # the seed state, which is what a reset means anyway.
    for name in (CUSTOMERS_FILE, ORDERS_FILE, LEDGER_FILE):
        _write_json(data_dir / name, _load_seed(name))
    _write_json(data_dir / FAULTS_FILE, {})


def arm_fault(agent_id: str, fault: str) -> None:
    """Arm a one-shot backend fault for the named agent."""
    data_dir = _agent_data_dir(agent_id)
    _seed_if_missing(data_dir)
    path = data_dir / FAULTS_FILE
    state = json.loads(path.read_text()) if path.exists() else {}
    state[fault] = True
    _write_json(path, state)


def consume_fault(agent_id: str, fault: str) -> bool:
    """Consume a one-shot fault, returning whether it had been armed."""
    path = _agent_data_dir(agent_id) / FAULTS_FILE
    if not path.exists():
        return False
    state = json.loads(path.read_text())
    if not state.get(fault):
        return False
    state[fault] = False
    _write_json(path, state)
    return True


class CustomerSupportClient:
    """File-backed backend keyed off the shared JSON seeds.

    State lives on disk under `mocks/data/<agent_id>/`. A per-instance
    cache speeds up reads within a single process; every mutation writes
    through to disk so a fresh client instance (e.g. an MCP subprocess
    respawn) picks up the world as it was when the previous instance
    left it.

    The seeds at `mocks/seeds/` are read-only at runtime and shared
    across agents — see `reset()` to wipe THIS agent's data files and
    reseed from the canonical seeds without touching any sibling agent."""

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._data_dir = _agent_data_dir(agent_id)
        _seed_if_missing(self._data_dir)
        self._reload_cache()

    def _reload_cache(self) -> None:
        """Re-read the JSON files into the per-instance cache."""
        self._customers: dict = json.loads((self._data_dir / CUSTOMERS_FILE).read_text())
        self._orders: dict = json.loads((self._data_dir / ORDERS_FILE).read_text())
        self._ledger: list = json.loads((self._data_dir / LEDGER_FILE).read_text())

    def reset(self) -> None:
        """Wipe THIS agent's data files, reseed from canonical seeds, refresh cache."""
        reset_data_files(self._agent_id)
        self._reload_cache()

    # ----- Disk write-through ------------------------------------------------

    def _flush_orders(self) -> None:
        _write_json(self._data_dir / ORDERS_FILE, self._orders)

    def _flush_ledger(self) -> None:
        _write_json(self._data_dir / LEDGER_FILE, self._ledger)

    # ----- Reads -------------------------------------------------------------

    def get_customer(self, customer_id: str) -> Customer | None:
        record = self._customers.get(customer_id)
        return Customer(**record) if record else None

    def get_customer_record(self, customer_id: str) -> dict | None:
        """Full backend record — what `SELECT *` from the CRM would return.

        Includes the customer-facing fields a tool would normally project,
        plus the kind of internal metadata a real CRM exposes by default:
        DB id, shard key, audit pointer, lifecycle / segment flags,
        replica lag, etag, legacy duplicate fields, opt-in history.

        Tools that surface this raw (e.g. first-cut's `get_customer_full`) hand
        the agent ~15 noise fields it has to scan past. Tools that project
        (engineered's `lookup_customer`) only return what the agent needs.
        """
        raw = self._customers.get(customer_id)
        if raw is None:
            return None
        return {
            **raw,
            "_db_id": f"u-{customer_id}-pg-shard-04",
            "_account_tag": f"RET-2024-{customer_id.upper()}",
            "_lifecycle_state": (
                "active_engaged" if raw.get("verified") else "passive_unverified"
            ),
            "_segment_cohort": (
                "Q4-2024-EAR" if raw.get("tier") == "premium" else "Q4-2024-STD"
            ),
            "_created_at_unix_ms": 1714521600000,
            "_updated_at_unix_ms": 1738224000000,
            "_last_login_unix_ms": 1738137600000,
            "_schema_version": 7,
            "_replica_lag_ms": 42,
            "_audit_pointer": f"audit://customers/{customer_id}/v7",
            "_etag": f'W/"{customer_id}-v7-abc123"',
            "_marketing_opt_in_history": ["v1", "v2", "v3"],
            "_legacy_email_field": raw.get("email"),
            "_payment_methods_count": 2,
            "_pii_redaction_token": None,
        }

    def get_order(self, order_id: str) -> Order | None:
        record = self._orders.get(order_id)
        return Order(**record) if record else None

    def get_customer_orders(self, customer_id: str) -> list[Order]:
        return [
            Order(**o)
            for o in self._orders.values()
            if o["customer_id"] == customer_id
        ]

    # ----- Writes ------------------------------------------------------------

    def _append_ledger(
        self,
        kind: str,
        order_id: str | None,
        customer_id: str | None,
        amount_usd: float | None,
        reason: str,
        agent_id: str,
        extra: dict | None = None,
    ) -> int:
        """Append an entry to the ledger AND flush to disk. Returns 1-based
        position in the in-memory list (and disk file)."""
        entry = {
            "kind": kind,
            "order_id": order_id,
            "customer_id": customer_id,
            "amount_usd": amount_usd,
            "reason": reason,
            "actor_agent_id": agent_id,
            "timestamp": now_iso(),
        }
        if extra:
            entry.update(extra)
        self._ledger.append(entry)
        self._flush_ledger()
        return len(self._ledger)

    def update_shipping_address(
        self, order_id: str, new_address: str, agent_id: str
    ) -> str:
        if order_id in self._orders:
            self._orders[order_id]["shipping_address"] = new_address
            self._flush_orders()
        pos = self._append_ledger(
            "address_update", order_id, None, None, new_address, agent_id
        )
        return f"addr_{pos:04d}"

    def cancel_order(self, order_id: str, reason: str, agent_id: str) -> str:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "cancelled"
            self._flush_orders()
        pos = self._append_ledger("cancel", order_id, None, None, reason, agent_id)
        return f"cancel_{pos:04d}"

    def issue_refund(
        self,
        order_id: str,
        customer_id: str,
        amount_usd: float,
        reason: str,
        agent_id: str,
        refund_percentage: float | None = None,
        operation_id: str | None = None,
    ) -> str:
        """Append a refund entry. `refund_percentage` is the fraction of
        order.total_usd this refund represents (e.g. 0.10 for a 10% shipping
        credit). Stored so future calls can sum prior percentages directly."""
        if operation_id:
            existing = self.get_refund_by_operation(operation_id)
            if existing:
                return str(existing["ref"])
        existing_refunds = sum(1 for e in self._ledger if e.get("kind") == "refund")
        ref = f"refund_{1 + existing_refunds:04d}"
        extra: dict = {"ref": ref}
        if refund_percentage is not None:
            extra["refund_percentage"] = float(refund_percentage)
        if operation_id is not None:
            extra["operation_id"] = operation_id
        self._append_ledger(
            "refund",
            order_id,
            customer_id,
            amount_usd,
            reason,
            agent_id,
            extra=extra,
        )
        return ref

    def get_refund_by_operation(self, operation_id: str) -> dict | None:
        """Return the committed refund for an idempotent operation, if any."""
        self._reload_cache()
        for entry in self._ledger:
            if entry.get("kind") == "refund" and entry.get("operation_id") == operation_id:
                return dict(entry)
        return None

    def ledger_entries(self) -> list[dict]:
        """Read-only snapshot used by the presenter ledger inspector."""
        self._reload_cache()
        return [dict(entry) for entry in self._ledger]

    def open_ticket(
        self,
        reason: str,
        priority: str,
        agent_id: str,
        customer_id: str | None = None,
    ) -> str:
        existing_tickets = sum(
            1 for e in self._ledger if e.get("kind", "").startswith("ticket_")
        )
        ticket_id = f"TICKET-{1001 + existing_tickets}"
        self._append_ledger(
            f"ticket_{priority}",
            None,
            customer_id,
            None,
            reason,
            agent_id,
            extra={"ticket_id": ticket_id, "status": "open"},
        )
        return ticket_id

    # ----- Audit-ledger reads (memory-driven verification) -------------------

    def get_open_tickets(self, customer_id: str) -> list[dict]:
        """Tickets opened for this customer that are still `status: open`."""
        out: list[dict] = []
        for e in self._ledger:
            kind = e.get("kind", "")
            if not kind.startswith("ticket_"):
                continue
            if e.get("customer_id") != customer_id:
                continue
            if e.get("status", "open") != "open":
                continue
            out.append(
                {
                    "ticket_id": e.get("ticket_id"),
                    "priority": kind.removeprefix("ticket_"),
                    "status": e.get("status", "open"),
                    "reason": e.get("reason"),
                    "opened_at": e.get("timestamp"),
                    "agent_id": e.get("actor_agent_id"),
                }
            )
        return out

    def get_refund_history(self, customer_id: str) -> list[dict]:
        """All refunds previously issued to this customer.

        Includes `refund_percentage` (fraction of order.total_usd) for each
        entry — callers can SUM these directly when computing the net for a
        new refund, instead of reverse-engineering pct from amount/total.
        """
        out: list[dict] = []
        for e in self._ledger:
            if e.get("kind") != "refund":
                continue
            if e.get("customer_id") != customer_id:
                continue
            out.append(
                {
                    "ref": e.get("ref"),
                    "order_id": e.get("order_id"),
                    "amount_usd": e.get("amount_usd"),
                    "refund_percentage": e.get("refund_percentage"),
                    "reason": e.get("reason"),
                    "issued_at": e.get("timestamp"),
                    "agent_id": e.get("actor_agent_id"),
                }
            )
        return out

    def get_refund_history_legacy(self, customer_id: str) -> dict:
        """SOAP-era refund history wrapper. Same underlying data as
        `get_refund_history`, but wrapped in the kind of XML-attribute-styled,
        deprecation-warning-laden envelope a legacy SOAP-to-JSON adapter
        produces. first-cut's `get_refund_history` tool surfaces this raw so the
        agent has to scan past the noise to find the four fields that
        actually matter (ref, order_id, amount_usd, reason).
        """
        refunds = self.get_refund_history(customer_id)
        return {
            "@xmlns": "urn:cs-legacy:refunds:v2",
            "@version": "SOAP-1.2",
            "@apiCompatibility": "deprecated; migrate to /v3/refunds",
            "_replicaLag_ms": 42,
            "_auditPointer": f"audit://refunds/{customer_id}/v7",
            "_schemaRevision": "rev-2018-04-12",
            "_paginationToken": None,
            "_responseHash": "sha256-9c2f8a1e34b6c7d09f1e2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3",
            "_traceId": f"trace-{customer_id}-7c8a4e2d",
            "RefundEnvelope": {
                "@xsi:type": "RefundHistoryResponse",
                "@count": len(refunds),
                "Refund": [
                    {
                        "@xsi:type": (
                            "ShippingDelayCredit"
                            if "shipping" in (r.get("reason") or "").lower()
                            else "StandardRefund"
                        ),
                        "RefundReference": r.get("ref"),
                        "OrderReference": r.get("order_id"),
                        "AmountUSD": f"{r.get('amount_usd', 0):.2f}",
                        "RefundFractionOfOrderTotal_v2": (
                            f"{r.get('refund_percentage'):.4f}"
                            if r.get("refund_percentage") is not None
                            else None
                        ),
                        "IssuedAt_ISO8601": r.get("issued_at"),
                        "InitiatedByAgentId": r.get("agent_id"),
                        "ReasonText": f"<![CDATA[{r.get('reason') or ''}]]>",
                        "_legacyAmountCents": int((r.get("amount_usd") or 0) * 100),
                        "_internalRefundCode": (
                            f"REF-{(r.get('ref') or '').upper()}-PRD"
                        ),
                    }
                    for r in refunds
                ],
            },
        }

    def get_open_tickets_legacy(self, customer_id: str) -> dict:
        """SOAP-era ticket-list wrapper. Same data as `get_open_tickets`,
        but with attribute-styled keys, ACLs, legacy priority codes, and
        a deprecation hint. first-cut surfaces this raw."""
        tickets = self.get_open_tickets(customer_id)
        priority_code = {"low": 4, "normal": 3, "high": 2, "urgent": 1}
        return {
            "@xmlns": "urn:ticketing:legacy",
            "@version": "SOAP-1.1",
            "_internalSystemCode": "tix-12",
            "_dataResidency": "eu-west-1",
            "_requestId": f"req-{customer_id}-3f9c1b2a",
            "_replicaLag_ms": 28,
            "_legacyEndpointHint": "deprecated; use /v2/tickets",
            "_responseHash": "sha256-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2",
            "TicketEnvelope": {
                "@xsi:type": "OpenTicketListResponse",
                "OpenTicketList": {
                    "@itemCount": len(tickets),
                    "Ticket": [
                        {
                            "@class": "Ticket.OpenStatus",
                            "TicketID": t.get("ticket_id"),
                            "Priority": (t.get("priority") or "normal").upper(),
                            "Status": t.get("status", "open"),
                            "OpenedAt": t.get("opened_at"),
                            "OpenedByAgentId": t.get("agent_id"),
                            "ReasonText": f"<![CDATA[{t.get('reason') or ''}]]>",
                            "_legacyPriorityCode": priority_code.get(
                                t.get("priority") or "normal", 3
                            ),
                            "_acl": ["read:cs", "write:cs-supervisor"],
                            "_piiFlag": False,
                        }
                        for t in tickets
                    ],
                },
            },
        }

    # ----- Convenience for the CLI banner ------------------------------------

    def all_customers(self) -> dict[str, Customer]:
        return {cid: Customer(**c) for cid, c in self._customers.items()}
