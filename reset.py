"""Reset the lab's persistent on-disk state — wipe non-seeded episodic memory.

Run this between demos / rehearsals to get back to the canonical starting
state:

    python reset.py

What it touches:

- cs_agent_v2/memory/episodic/customer_*.md  ← deleted, EXCEPT
                                                customer_cust_002.md (Bob's
                                                seeded memory, committed to
                                                git for the §3 demo)

What this does NOT touch:

- Mock customer/order/ledger state lives in-memory in each agent's running
  process and is re-seeded from `mocks/seeds/*.json` on startup.
- Conversation memory (`agent.messages` on each cached Agent) is in-process
  only — restart the agent processes or hit `/api/reset` to wipe it.

To clear the in-process state, either:

  - restart the agent process(es), or
  - click "reset" in the web UI (hits both agents' `/api/reset`), or
  - `curl -X POST http://localhost:8001/api/reset` and `:8002` directly.

If Bob's memory file got modified by `compact_memory` during a demo,
restore it via:

    git restore cs_agent_v2/memory/episodic/customer_cust_002.md
"""

from pathlib import Path

ROOT = Path(__file__).parent
EPISODIC_DIR = ROOT / "cs_agent_v2" / "memory" / "episodic"

# Bob's memory is the one seeded file we keep — committed to git.
KEEP_MEMORY_FILES = {"customer_cust_002.md"}


def reset_memory() -> list[str]:
    """Remove non-seeded customer memory files under cs_agent_v2/memory/episodic/."""
    if not EPISODIC_DIR.exists():
        return []
    removed = []
    for f in EPISODIC_DIR.glob("customer_*.md"):
        if f.name in KEEP_MEMORY_FILES:
            continue
        f.unlink()
        removed.append(str(f.relative_to(ROOT)))
    return removed


def main() -> None:
    print("Resetting lab state…\n")

    removed = reset_memory()
    for path in removed:
        print(f"  ✗ removed {path}")

    print("\nDone.")
    print("Note: mock customer/order/ledger state and conversation memory")
    print("live in-process in the running agent services. Restart them, hit")
    print("/api/reset, or click 'reset' in the web UI to wipe.")


if __name__ == "__main__":
    main()
