"""Session token budget for the live agent harnesses.

The resource worth metering in an agent loop is model tokens, not tool calls:
a tool call costs nothing by itself, the model call that reads its observation
is what burns context and money. So the meter reads the provider's own usage
numbers and the decision is taken at the only place that can actually stop the
spend — immediately before the next model call, via `BeforeModelCallEvent`.

The budget counts the agent loop's total provider usage: input plus output.
Input for the next call can be projected before dispatch, so the guard can stop
before re-sending a context that no longer fits. Planner, reviewer and wrap-up
calls are harness work and remain outside this loop budget. The policy MCP's
one-time internal lookup is deliberately disregarded for this demo.

    hard      (first-cut)   the next model call is cancelled outright. The turn
                            ends mid-task with an error and nothing to resume.

    graceful  (engineered)  the loop stops at 90%, leaving the last tenth of the
                            budget and asks before continuing. The status call
                            is not part of the loop: `budget_wrapup.py`
                            makes it with no tools attached, so it can only
                            write prose — the status so far plus a yes/no
                            question about continuing. A "yes" adds a grant to
                            the same session meter.

This is deliberately NOT a tool guard. Cancelling tool calls and letting the
loop keep running would spend more tokens after the budget said stop, and it
would leave the wrap-up to a model that is still holding tools and may just
call another one. Stopping the loop and then making one bounded, tool-free
call is what makes the last response predictable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from strands.hooks import HookRegistry
from strands.hooks.events import (
    AfterModelCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
)

from loop_state import (
    RunState,
    TokenSplit,
    record_tool_call,
    loop_tokens_used,
    token_ceiling,
    token_warning_threshold,
)

# Placed in the conversation by Strands when the guard cancels a model call.
# The harness swaps it for the real wrap-up reply before the turn ends, so it
# never reaches the customer or the next turn's context.
BUDGET_STOP_MARKER = "[token budget guard: loop stopped before this model call]"

def _call_usage(event: AfterModelCallEvent) -> TokenSplit:
    """Tokens billed for the call that just returned, kept split.

    Read from the assistant message's own metadata rather than from
    `agent.event_loop_metrics`: Strands attaches the usage to the message
    before this hook runs but accumulates it into the agent's metrics only
    afterwards, so the metrics lag by exactly one call. The message is also
    per-call, which keeps the run's total independent of an agent instance
    shared with other runs.
    """
    response = event.stop_response
    message = getattr(response, "message", None) or {}
    usage = (message.get("metadata") or {}).get("usage") or {}

    def _field(name: str) -> int:
        try:
            return int(usage.get(name, 0))
        except (AttributeError, TypeError, ValueError):
            return 0

    return TokenSplit(
        input_tokens=_field("inputTokens"), output_tokens=_field("outputTokens")
    )


@dataclass
class TokenBudgetHook:
    """Meter real token spend without knowing which demo prompt is active."""

    mode: Literal["hard", "graceful"]

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(AfterModelCallEvent, self._after_model)
        registry.add_callback(BeforeToolCallEvent, self._before_tool)

    # -- the decision point: one model call from now -----------------------

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        run: RunState | None = getattr(event.agent, "_active_run_state", None)
        if run is None:
            return
        # The next call's input is known before dispatch and is the best
        # available projection of whether another iteration fits.
        projected_input = int(event.projected_input_tokens or 0)
        run.projected_next_call_tokens = projected_input
        run.peak_call_input_tokens = max(run.peak_call_input_tokens, projected_input)
        run.model_call_count += 1

        spent = loop_tokens_used(run)
        projected_total = spent + projected_input
        ceiling = token_ceiling(run)

        if run.model_call_count == 1 and spent == 0:
            # Bootstrap a new session even when an intentionally tiny demo
            # budget is below the first prompt's context size.
            return

        if self.mode == "hard":
            if projected_total >= ceiling:
                run.status = "error"
                run.next_loop_state = "BUDGET_EXHAUSTED"
                run.exit_reason = "TOKEN_BUDGET_EXCEEDED"
                run.blocker = "token_budget_exceeded"
                # `cancel` ends the turn with this text as the assistant
                # message, so the failure is what the customer is left with.
                spent_calls = run.model_call_count - 1
                event.cancel = (
                    f"Token budget exceeded: this session has used {spent:,} of "
                    f"{ceiling:,} allowed tokens; the next context would require "
                    f"another {projected_input:,}. {spent_calls} model "
                    f"call{'' if spent_calls == 1 else 's'}. The task is unfinished "
                    "and no state was saved."
                )
            return

        # Stop at the warning line or when the next context would cross the
        # ceiling. The tool-free wrap-up is harness work and is excluded.
        if spent < token_warning_threshold(run) and projected_total < ceiling:
            return

        # The guard line is reached. The loop does not get this call; the
        # harness spends what is left on the wrap-up instead, which ends by
        # asking whether to buy another grant (see budget_wrapup.py).
        run.budget_wrapup = True
        run.status = "waiting"
        run.next_loop_state = "ASK"
        run.exit_reason = "PAUSED_AWAITING_USER_CONTINUE"
        run.blocker = "token_budget_guard"
        run.resume_condition = (
            f"customer approves another {run.token_budget:,}-token grant"
        )
        event.cancel = BUDGET_STOP_MARKER

    def _after_model(self, event: AfterModelCallEvent) -> None:
        """Add what the call actually cost, which `_before_model` could only
        project. A cancelled call carries no usage and adds nothing."""
        run: RunState | None = getattr(event.agent, "_active_run_state", None)
        if run is None:
            return
        usage = _call_usage(event)
        run.output_tokens += usage.output_tokens
        run.input_tokens += usage.input_tokens
        if usage.total:
            # The provider exposes only total input, not a component breakdown.
            # Anchor the fixed surface to the first completed call: at that
            # point the only dynamic content in a fresh agent is the first user
            # message. We estimate just that small message, subtract it from the
            # exact provider total, then reuse the resulting baseline.
            fixed_baseline = getattr(event.agent, "_fixed_context_baseline_tokens", None)
            if fixed_baseline is None:
                first_message = max(
                    0,
                    int(getattr(event.agent, "_current_user_message_token_estimate", 0) or 0),
                )
                fixed_baseline = max(0, usage.input_tokens - first_message)
                setattr(event.agent, "_fixed_context_baseline_tokens", fixed_baseline)
            fixed = min(usage.input_tokens, max(0, int(fixed_baseline)))
            run.model_call_usage.append(
                {
                    "call": len(run.model_call_usage) + 1,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total,
                    "fixed_context_tokens": fixed,
                    "dynamic_context_tokens": max(0, usage.input_tokens - fixed),
                }
            )

    # -- tool calls: metered, never gated ---------------------------------

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        """Count dispatches. The budget is not enforced here on purpose — a
        cancelled tool call still leaves the loop running and still costs a
        model call to read the cancellation."""
        run: RunState | None = getattr(event.agent, "_active_run_state", None)
        if run is None:
            return
        tool_use = event.tool_use
        record_tool_call(
            run, str(tool_use.get("name", "tool")), tool_use.get("input") or {}
        )
