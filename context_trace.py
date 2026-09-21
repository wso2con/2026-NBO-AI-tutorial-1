"""Capture the exact inputs visible immediately before every model call."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from strands.hooks import HookOrder, HookRegistry
from strands.hooks.events import BeforeModelCallEvent


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


@dataclass
class ContextTraceHook:
    """Append a serializable context snapshot to the invoking Agent."""

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        # Last, so the snapshot is the context that actually goes out: the
        # engineered loop's context pipeline may compact at this same event,
        # and a snapshot taken before it would show messages the model never
        # saw. Harmless in the first-cut loop, which has no pipeline.
        registry.add_callback(
            BeforeModelCallEvent, self._capture, order=HookOrder.DEFAULT + 20
        )

    def _capture(self, event: BeforeModelCallEvent) -> None:
        agent = event.agent
        specs = agent.tool_registry.get_all_tool_specs()
        system_prompt = str(agent.system_prompt or "")
        snapshots = getattr(agent, "_context_trace_snapshots", None)
        if snapshots is None:
            snapshots = []
            setattr(agent, "_context_trace_snapshots", snapshots)
        snapshots.append(
            {
                "messages": _json_safe(getattr(agent, "messages", []) or []),
                "system_prompt": system_prompt,
                "tool_contracts": _json_safe(specs),
                "projected_input_tokens": event.projected_input_tokens,
            }
        )
