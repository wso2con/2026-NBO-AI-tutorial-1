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
  used by `agent/core.py` to prepend stored notes to the first user message
  of a session and by `reset.py` for demo cleanup.
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
    """Append one useful note for a future customer session.

    Call this at most once near the end of a turn, and only when the turn
    created cross-session context that read tools will not preserve: an
    unresolved promise, deadline, time-sensitive customer need, relevant
    preference/constraint, or recurring pattern. The context may be temporary;
    it only needs to remain useful if the customer returns.

    A customer-provided deadline, such as a flight tomorrow, is appropriate
    when no tool stores it. Do not store a conversation summary or facts that
    tools can retrieve, such as contact details, current order/ticket status,
    refund details, policy rules, or procedural instructions.

    `note` should usually be one short sentence (under 300 characters): state
    what may matter later and when it expires or should be revisited. IDs are
    pointers only; say they must be verified. Example: "Customer flies at
    08:00 on 2026-09-23 and needs #1242 beforehand; verify delivery and any
    open ticket if they return before departure."

    The harness binds the customer. Pass an empty string for `customer_id`.
    Do not call this when a memory_control block requires compact_memory.

    Args:
        customer_id: Always pass "". The harness replaces it with the current
            authenticated customer ID.
        note: One short cross-session fact, deadline, or constraint, preferably
            under 300 characters. Do not include a routine session summary."""
    append(customer_id, note)
    return {"ok": True, "saved_for": customer_id}


@tool
def compact_memory(customer_id: str, new_content: str) -> dict:
    """Replace the entire episodic-memory file with a concise rewrite.

    Call only when the harness supplied `<memory_control action="compact">`.
    This overwrites all stored memory, so `new_content` must be complete and
    self-contained Markdown. Preserve unresolved promises, exact dates and ID
    pointers, recurring patterns, and stable preferences. Remove duplication,
    routine summaries, tool results, current statuses, and policy/procedure
    text. Include any new durable context created in the current turn.

    Call this once instead of append_memory. The harness binds the customer;
    pass an empty string for `customer_id`.

    Args:
        customer_id: Always pass "". The harness replaces it with the current
            authenticated customer ID.
        new_content: The complete replacement memory as concise Markdown, not
            a patch or only the newest note."""
    write(customer_id, new_content)
    return {"ok": True, "rewrote": customer_id}
