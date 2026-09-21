"""Reset the lab's persistent on-disk state — mock backend plus episodic memory.

Run this between demos / rehearsals to get back to the canonical starting
state:

    python reset.py

What it touches:

- mocks/data/<agent_id>/*.json                 ← restored from mocks/seeds/,
                                                for BOTH agents, including the
                                                armed-fault file
- cs_agent_engineered/memory/episodic/customer_*.md  ← deleted, EXCEPT
                                                customer_cust_002.md (Bob's
                                                seeded memory, committed to
                                                git for the §3 demo)

What this does NOT touch:

- Conversation memory (`agent.messages` on each cached Agent) is in-process
  only — restart the agent processes or hit `/api/reset` to wipe it.

To clear the in-process state, either:

  - restart the agent process(es), or
  - click "reset" in the web UI (hits both agents' `/api/reset`), or
  - `curl -X POST http://localhost:8001/api/reset` and `:8002` directly.

Running this while the services are up is safe — `reset_data_files` replaces
each file atomically — but the agents keep their conversation memory, so the
UI's reset button is still the better between-scenario move.

If Bob's memory file got modified by `compact_memory` during a demo,
restore it via:

    git restore cs_agent_engineered/memory/episodic/customer_cust_002.md
"""

from pathlib import Path

from mocks.client import reset_data_files

ROOT = Path(__file__).parent
EPISODIC_DIR = ROOT / "cs_agent_engineered" / "memory" / "episodic"

# Each agent owns a data dir named after its agent id. These must stay in step
# with `cs_agent_first_cut/config.py`'s AGENT_ID default and the `agent.id`
# field in `cs_agent_engineered/agent-profile.yaml` — nothing at import time
# can check that for us, because the services are not running here.
AGENT_IDS = ("cs-agent-first-cut", "cs-agent-engineered")

# Bob's memory is the one seeded file we keep — committed to git.
KEEP_MEMORY_FILES = {"customer_cust_002.md"}


def reset_mocks() -> list[str]:
    """Restore every agent's mock customer/order/ledger/fault files from seeds."""
    restored = []
    for agent_id in AGENT_IDS:
        reset_data_files(agent_id)
        restored.append(f"mocks/data/{agent_id}/")
    return restored


def reset_memory() -> list[str]:
    """Remove non-seeded customer memory files under cs_agent_engineered/memory/episodic/."""
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

    for path in reset_mocks():
        print(f"  ↺ reseeded {path}")

    removed = reset_memory()
    for path in removed:
        print(f"  ✗ removed {path}")

    print("\nDone.")
    print("Note: conversation memory lives in-process in the running agent")
    print("services. Restart them, hit /api/reset, or click 'reset' in the")
    print("web UI to wipe it.")


if __name__ == "__main__":
    main()
