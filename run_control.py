"""Generic tool-dispatch controls used by the live agent harnesses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from strands.hooks import HookRegistry
from strands.hooks.events import BeforeToolCallEvent

from loop_state import RunState, grace_threshold_calls, record_tool_call


@dataclass
class ToolBudgetHook:
    """Meter real tool dispatches without knowing which demo prompt is active."""

    mode: Literal["hard", "graceful"]

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        run: RunState | None = getattr(event.agent, "_active_run_state", None)
        if run is None:
            return
        limit = (
            grace_threshold_calls(run)
            if self.mode == "graceful"
            else run.max_tool_calls
        )
        tool_use = event.tool_use
        name = str(tool_use.get("name", "tool"))
        inputs = tool_use.get("input") or {}
        if run.tool_call_count >= limit:
            if self.mode == "graceful":
                run.status = "paused"
                run.next_loop_state = "PAUSE"
                run.exit_reason = "PAUSED_AT_BUDGET_GUARD"
                run.resume_condition = "start_next_turn_with_fresh_tool_budget"
                error = {
                    "error": "budget_pause",
                    "code": 429,
                    "detail": "The 90% tool-call guard was reached before this dispatch.",
                    "remediation": run.resume_condition,
                }
            else:
                run.status = "error"
                run.next_loop_state = "BUDGET_EXHAUSTED"
                run.exit_reason = "TOOL_BUDGET_EXHAUSTED"
                error = {
                    "error": "tool_budget_exhausted",
                    "code": 429,
                    "detail": "The per-turn tool-call budget was exhausted.",
                }
            event.cancel_tool = json.dumps(error)
            return
        record_tool_call(run, name, inputs)
