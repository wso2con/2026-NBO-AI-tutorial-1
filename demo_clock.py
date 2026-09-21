"""Fixed demo clock — the single source of truth for "now" in this lab.

An LLM has no clock. Unless the harness tells it what day it is, it either
assumes its training cutoff or ignores time entirely — and then an order
`preparing` since May reads as absurd in September, a "due today" delivery
reads as months overdue, and the agent escalates instead of acting.

Rather than let the demo drift as the wall clock advances past the fixtures,
the whole lab runs on one pinned date. The seed data in `mocks/seeds/` is
authored relative to `DEMO_TODAY`, and every surface that needs a date reads
it from here:

  - `cs_agent_first_cut/agent.py`  — `Today's date` line in the system prompt
  - `cs_agent_engineered/agent-profile.yaml` — `{today}` placeholder, filled
    by `agent/core.py::_build_instructions`
  - `planner.py`                   — `Today's date` line in the planner prompt
  - `mocks/client.py`              — ledger entry timestamps
  - `cs_agent_engineered/agent/memory.py` — episodic memory entry headings

Override with the `DEMO_TODAY` env var (ISO `YYYY-MM-DD`) if you re-author the
fixtures. Changing it without moving the seed dates will desync the scenarios.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

# The pinned "today" for every agent, tool, and fixture in the lab.
DEMO_TODAY = os.environ.get("DEMO_TODAY", "2026-09-22")


def today() -> date:
    """The pinned demo date as a `date`."""
    return date.fromisoformat(DEMO_TODAY)


def today_iso() -> str:
    """The pinned demo date as `YYYY-MM-DD` — for prompts."""
    return DEMO_TODAY


def now() -> datetime:
    """The pinned demo date with the real wall-clock time of day, UTC.

    Ordering within a demo run stays truthful (later writes get later
    timestamps) while the date component stays consistent with the fixtures.
    """
    wall = datetime.now(UTC)
    return wall.replace(year=today().year, month=today().month, day=today().day)


def now_iso() -> str:
    """Pinned timestamp as `YYYY-MM-DDTHH:MM:SS...Z` — for ledger entries."""
    return now().isoformat().replace("+00:00", "Z")


def stamp() -> str:
    """Pinned timestamp as `YYYY-MM-DD HH:MM` — for memory entry headings."""
    return now().strftime("%Y-%m-%d %H:%M")
