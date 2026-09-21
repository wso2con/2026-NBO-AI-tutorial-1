"""File-based episodic memory.

Each customer's prior interactions are stored as a markdown file at
`memory/episodic/customer_<id>.md`. When `memory.episodic.enabled` is true in
agent-profile.yaml, the file is injected into the FIRST USER MESSAGE of a new
session by `agent/core.py::prepend_memory` — not into the system prompt, which
carries only the protocol from `_memory_protocol()`. Conversation history
(within-session chat) is separate — it's `agent.messages` on the per-customer
cached Agent in `main.py`, no on-disk transcript.

Same pattern as Claude Code's `CLAUDE.md` + `MEMORY.md`, GitHub Copilot's
`.github/copilot-instructions.md`, ChatGPT's bio. Production agents with
larger memory volumes swap this for vector retrieval (Mem0, Letta, Zep);
for one customer's history (a few dozen lines) full-load is the right call.

Two layers live here:
- The `load` / `append` / `write` / `clear` helpers — pure file ops, also
  used by `agent/core.py` to inject memory into the system prompt at build
  time and by `reset.py` for demo cleanup.
- The `append_memory` / `compact_memory` Strands tools — `@tool`-decorated
  wrappers the agent calls during the loop. They live here (in-process
  with the agent) rather than in an MCP server because memory is
  session-scoped state the agent owns end-to-end — there's no team
  boundary to cross. See README §2b's "tools that stay local" rule.
"""

from pathlib import Path

from strands import tool

from demo_clock import stamp

MEMORY_DIR = Path(__file__).parent.parent / "memory" / "episodic"


class InvalidCustomerId(ValueError):
    """Raised when a customer_id cannot be used as a memory filename."""


def _path(customer_id: str) -> Path:
    """Resolve a customer's memory file, refusing anything that escapes MEMORY_DIR.

    `customer_id` reaches this module from the HTTP layer (`/api/memory`) and
    from model-proposed tool arguments, so it is untrusted input. Without this
    check a value like `../../secrets` would resolve outside the memory
    directory and turn a memory read into an arbitrary file read.
    """
    if not customer_id or Path(customer_id).name != customer_id:
        raise InvalidCustomerId(f"invalid customer_id: {customer_id!r}")
    path = (MEMORY_DIR / f"customer_{customer_id}.md").resolve()
    if MEMORY_DIR.resolve() not in path.parents:
        raise InvalidCustomerId(f"invalid customer_id: {customer_id!r}")
    return path


def load(customer_id: str) -> str:
    """Return the customer's full memory file contents (empty string if none)."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(customer_id)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def append(customer_id: str, note: str) -> None:
    """Append a timestamped entry to the customer's memory file."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(customer_id)
    timestamp = stamp()
    entry = f"\n## {timestamp}\n{note}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(entry)


def write(customer_id: str, content: str) -> None:
    """Overwrite the customer's memory file (compaction)."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _path(customer_id).write_text(content, encoding="utf-8")


def clear(customer_id: str) -> None:
    """Remove a customer's memory file. Used for demo reset, not by agent."""
    p = _path(customer_id)
    if p.exists():
        p.unlink()


# ----- Agent-facing tools -------------------------------------------------


@tool
def append_memory(customer_id: str, note: str) -> dict:
    """After an interaction with the customer, append a note to their memory file with the information that we cannot get from tools.
    This is to provide a personalized experience for the customer and help you to build a better understanding of customer requirements. 
    But this is Episodic memory, not a scratchpad — only record what tools cannot tell the next agent. Notes should be concise, like a sticky note.

    Record only what tools CANNOT tell the next agent:
      - **Open promises** ("Told Bob a replacement would ship by 2026-09-25;
        track TICKET-1042.")
      - **Behavioural patterns / tone** ("Second damaged delivery to this
        address in 6 months — fulfillment-side pattern, not customer-side.")
      - **Pointers for next time** ("If she follows up on #1234, trace +
        escalate per policy.")

    Do NOT record:
      - Refund amounts or refs (the ledger has them; `get_refund_history`)
      - Ticket IDs as standalone facts (use `get_open_tickets` to verify
        status — memory may be stale)
      - Customer tier / tenure / contact (use `lookup_customer`)
      - Order status / lateness / damage flag (use `get_order`)
      - Policy citations (use `check_policy`)

    Concrete example — Alice complained #1234 was late; you issued a $10
    shipping_delay credit.

      ❌ BAD: "Customer reported order #1234 late. Verified order is
              in_transit_delayed with DHL, 4 business days late. No prior
              refunds on file. Issued $10 shipping_delay_credit per policy
              with ref recorded in ledger. Set expectation to keep tracking;
              will follow up if not delivered soon."

      ✅ GOOD: "#1234 first delay complaint. Told her 1–2 more days.
               Tone: reasonable."

    The BAD version is ~5x longer and every fact in it is verifiable via
    a tool call. The GOOD version captures only the promise made and the
    tone — the two things tools can't surface. Procedural follow-up advice
    ("if she follows up, trace + escalate per policy") is policy recap and
    does NOT belong here — `handle-refund` + `check_policy` cover it.

    The customer is bound by the system; pass "" for `customer_id`."""
    append(customer_id, note)
    return {"ok": True, "saved_for": customer_id}


@tool
def compact_memory(customer_id: str, new_content: str) -> dict:
    """Rewrite the customer's entire memory file with a compacted version.

    Use ONLY when the existing memory has grown long and rambling. You
    are OVERWRITING; old content is replaced. Be careful.
    """
    write(customer_id, new_content)
    return {"ok": True, "rewrote": customer_id}
