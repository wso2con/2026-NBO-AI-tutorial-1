"""Observable execution state for the conference demo.

This module deliberately contains no model logic.  It records what the
harness can prove from requests, tool calls, observations and termination.
Conversation history and episodic memory remain owned by the agents.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import floor
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

LoopDecision = Literal[
    "CONTINUE",
    "ASK",
    "WAIT",
    "PAUSE",
    "RECOVER",
    "COMPLETE",
    "STOP",
    "ESCALATE",
    "BUDGET_EXHAUSTED",
]


@dataclass(frozen=True)
class TokenSplit:
    """One model call's usage, kept split at every boundary it crosses.

    Both halves count toward the loop budget. Keeping them split makes the
    context cost (input) and generated work (output) visible independently.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def openai_usage(response: Any) -> TokenSplit:
    """Split an OpenAI chat-completion response's usage.

    Reasoning tokens are billed as completion tokens and are left inside
    `output_tokens` on purpose: they are work the model did, and hiding them
    would make the meter understate a reasoning model's real spend.
    """
    usage = getattr(response, "usage", None)
    return TokenSplit(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


@dataclass
class OperationState:
    operation_id: str
    status: str = "pending"
    external_status: str | None = None


@dataclass
class RunState:
    run_id: str
    scenario_id: str | None
    customer_id: str
    goal: str
    status: str = "running"
    next_loop_state: LoopDecision = "CONTINUE"
    blocker: str | None = None
    progress: dict[str, str] = field(default_factory=dict)
    operations: dict[str, OperationState] = field(default_factory=dict)
    success_criteria: list[str] = field(default_factory=list)
    iteration_count: int = 0
    tool_call_count: int = 0
    # Model calls actually started this turn, counted by the budget guard at
    # the point it can still stop them (`iteration_count` is the trace view,
    # drained asynchronously by the service).
    model_call_count: int = 0
    # Exact provider-reported usage for completed model calls in this turn.
    # This powers the context chart; projected estimates are kept only for the
    # pre-call budget decision.
    model_call_usage: list[dict[str, int]] = field(default_factory=list)
    max_iterations: int = 6
    # The budget is denominated in total loop tokens: input plus output.
    # The budget belongs to the TASK, not to one turn. `token_budget` is the
    # size of a single grant; the ceiling is one grant plus one more for every
    # extension the customer has approved (`token_ceiling`). The meter
    # accumulates across every turn of the task, so a "yes" resumes into a
    # bigger ceiling rather than a cleared meter — otherwise work could never end.
    token_budget: int = 40_000
    budget_grants: int = 0
    # Summed from each model call's own reported usage, so both survive multiple
    # turns of one task and an agent instance shared with other runs.
    # Harness-side model calls are intentionally outside this loop budget.
    output_tokens: int = 0
    input_tokens: int = 0
    # Largest single call's input, used to show context growth across calls.
    peak_call_input_tokens: int = 0
    projected_next_call_tokens: int = 0
    # Tokens spent by model calls the harness makes around the loop — the
    # planner, the budget wrap-up, the policy evaluator. They are real spend on
    # the turn, so they are reported, but they are not what the budget meters.
    auxiliary_output_tokens: int = 0
    auxiliary_input_tokens: int = 0
    budget_warning_ratio: float = 0.9
    # Set by the graceful guard when the warning threshold is crossed: no more
    # tool dispatches, one last model call to report status and ask the human.
    budget_wrapup: bool = False
    resume_condition: str | None = None
    pending_request: str | None = None
    exit_reason: str | None = None
    event_count: int = 0
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    evaluation_status: str = "pending"
    contract_source: str | None = None
    loaded_skills: list[str] = field(default_factory=list)
    ordering_constraints: list[dict[str, str]] = field(default_factory=list)
    action_preconditions: list[dict[str, str]] = field(default_factory=list)

    def dump(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload.get("scenario_id") is None:
            payload.pop("scenario_id", None)
        payload["token_ceiling"] = token_ceiling(self)
        payload["token_budget_used_percent"] = (
            round(100 * loop_tokens_used(self) / token_ceiling(self))
            if token_ceiling(self)
            else 100
        )
        payload["tokens_remaining"] = max(0, token_ceiling(self) - loop_tokens_used(self))
        payload["token_warning_threshold"] = token_warning_threshold(self)
        return payload


class RunStore:
    """Small process-local run registry used by each demo service."""

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = RLock()

    def start(
        self,
        *,
        run_id: str | None,
        customer_id: str,
        goal: str,
        token_budget: int | None = None,
        scenario_id: str | None = None,
        resume: bool = False,
        continue_turn: bool = False,
    ) -> RunState:
        """Begin a turn.

        A repeated run id is the same chat session, so its token meter carries
        across ordinary turns. `resume=True` additionally marks a continuation
        after a human-approved budget grant. A new run id starts from zero.

        `continue_turn=True` says this call is not a new turn at all: it picks
        up a turn that stopped mid-flight, as a human-confirmation resume does
        when the parked tool call finally runs. The per-turn counters —
        iterations, tool calls, and the per-model-call usage the console draws
        its context bars from — then carry on instead of restarting, so the
        work done before the pause and the work done after it are reported as
        the one turn they are. Without it the bars redraw from the resume and
        the model calls that led up to the confirmation vanish from the turn
        that made them.
        """
        key = run_id or str(uuid4())
        with self._lock:
            run = self._runs.get(key)
            is_new = run is None
            if is_new:
                run = RunState(key, scenario_id, customer_id, goal)
                self._runs[key] = run
                resume = False
            else:
                run.status = "running"
                run.next_loop_state = "CONTINUE"
                run.exit_reason = None
            if not continue_turn:
                run.iteration_count = 0
                run.tool_call_count = 0
                run.model_call_count = 0
                run.model_call_usage = []
                run.projected_next_call_tokens = 0
            # Always cleared: the guard's verdict belongs to the model call
            # about to happen, not to the one that set it.
            run.budget_wrapup = False
            if is_new:
                run.token_budget = max(1_000, min(int(token_budget or 40_000), 500_000))
                run.budget_grants = 0
                run.output_tokens = 0
                run.input_tokens = 0
                run.peak_call_input_tokens = 0
                run.auxiliary_output_tokens = 0
                run.auxiliary_input_tokens = 0
            elif not resume and token_budget is not None:
                # Let the presenter change the session ceiling without erasing
                # the spend already visible against it.
                run.token_budget = max(1_000, min(int(token_budget), 500_000))
            return run

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


def event(
    event_type: str, run: RunState, summary: str, **detail: Any
) -> dict[str, str]:
    """Create one SSE frame using the lab's existing JSON-over-SSE shape."""
    run.event_count += 1
    payload = {"run_id": run.run_id, "summary": summary, **detail}
    import json

    return {"event": event_type, "data": json.dumps(payload)}


def apply_task_contract(run: RunState, contract: dict[str, Any], source: str) -> None:
    """Register a reusable procedure contract discovered at runtime.

    Contracts come from enabled Skills, never from a selected demo scenario.
    The Skill explains the procedure to the model while this structured view
    gives the harness observable completion and ordering rules.
    """
    goal = str(contract.get("goal", "")).strip()
    if goal:
        run.goal = goal
    for criterion in contract.get("required_criteria", []) or []:
        name = str(criterion).strip()
        if name and name not in run.success_criteria:
            run.success_criteria.append(name)
    for constraint in contract.get("ordering", []) or []:
        if not isinstance(constraint, dict):
            continue
        normalized = {
            "before": str(constraint.get("before", "")).strip(),
            "after": str(constraint.get("after", "")).strip(),
            "violation": str(constraint.get("violation", "ordering_violation")).strip(),
        }
        if normalized["before"] and normalized["after"] and normalized not in run.ordering_constraints:
            run.ordering_constraints.append(normalized)
    for precondition in contract.get("action_preconditions", []) or []:
        if not isinstance(precondition, dict):
            continue
        normalized = {
            "tool": str(precondition.get("tool", "")).strip(),
            "requires": str(precondition.get("requires", "")).strip(),
            "violation": str(precondition.get("violation", "missing_action_precondition")).strip(),
        }
        if normalized["tool"] and normalized["requires"] and normalized not in run.action_preconditions:
            run.action_preconditions.append(normalized)
    run.contract_source = source
    skill_name = source.removeprefix("skill:")
    if skill_name and skill_name not in run.loaded_skills:
        run.loaded_skills.append(skill_name)


def record_iteration(run: RunState) -> None:
    run.iteration_count += 1


def record_tool_call(
    run: RunState, tool_name: str, args: dict[str, Any] | None = None
) -> None:
    run.tool_call_count += 1
    run.progress[f"called:{tool_name}"] = "done"
    run.tool_history.append(
        {
            "name": tool_name,
            "args": dict(args or {}),
            "result": None,
            "is_error": None,
        }
    )


def record_observation(
    run: RunState,
    *,
    tool_name: str,
    observation: Any,
    is_error: bool = False,
) -> None:
    """Record backend evidence that can satisfy harness postconditions."""
    run.progress["latest_tool"] = tool_name
    run.progress["latest_observation"] = "error" if is_error else "observed"
    for item in reversed(run.tool_history):
        if item["name"] == tool_name and item["result"] is None:
            item["result"] = observation
            item["is_error"] = is_error
            break
def usage_payload(run: RunState) -> dict[str, Any]:
    """Token spend for the task so far, reported alongside the agent's reply.

    The governed total is loop input plus loop output. Harness-side planner,
    reviewer and wrap-up calls remain available as diagnostics but are excluded
    from the visible loop budget; the one-time policy MCP lookup is not tracked.

    Cumulative, not per turn: a task that has been continued twice reports
    everything it has spent against the ceiling those continuations bought.
    """
    ceiling = token_ceiling(run)
    loop_total = loop_tokens_used(run)
    return {
        "loop_total_tokens": loop_total,
        "loop_generated_tokens": run.output_tokens,
        "auxiliary_generated_tokens": run.auxiliary_output_tokens,
        "total_generated_tokens": run.output_tokens + run.auxiliary_output_tokens,
        "loop_input_tokens": run.input_tokens,
        "auxiliary_input_tokens": run.auxiliary_input_tokens,
        "total_input_tokens": run.input_tokens + run.auxiliary_input_tokens,
        "peak_call_input_tokens": run.peak_call_input_tokens,
        "token_budget": ceiling,
        "base_token_budget": run.token_budget,
        "budget_grants": run.budget_grants,
        "budget_used_percent": (
            round(100 * loop_total / ceiling) if ceiling else 100
        ),
        "model_calls": len(run.model_call_usage),
        "model_call_usage": run.model_call_usage,
        "tool_calls": run.tool_call_count,
    }


def add_auxiliary(run: RunState, usage: TokenSplit) -> None:
    """Record one harness-side model call: reported, never metered."""
    run.auxiliary_input_tokens += usage.input_tokens
    run.auxiliary_output_tokens += usage.output_tokens


def token_ceiling(run: RunState) -> int:
    """Total loop tokens this session may spend."""
    return run.token_budget * (1 + run.budget_grants)


def loop_tokens_used(run: RunState) -> int:
    """Input plus output used by the agent loop, excluding harness-side calls."""
    return run.input_tokens + run.output_tokens


def grant_budget(run: RunState) -> int:
    """Extend the task's ceiling by one more grant. Returns the new ceiling.

    Called only when a human has approved the extension. The model cannot
    reach this, which is the whole point of putting the decision here.
    """
    run.budget_grants += 1
    return token_ceiling(run)


def token_warning_threshold(run: RunState) -> int:
    """Total-token count at which the graceful guard stops starting new work."""
    return max(1, floor(token_ceiling(run) * run.budget_warning_ratio))


def context_snapshot(
    *,
    variant: str,
    prompt: str,
    system_prompt: str,
    message_count: int,
    tool_names: list[str],
    plan: str = "",
    memory: str = "",
    execution_state_selected: bool = False,
) -> dict[str, Any]:
    """Describe context actually assembled by the harness, without token claims."""
    return {
        "variant": variant,
        "request": prompt,
        "context_lifetime": "this model call",
        "conversation_messages": message_count,
        "system_prompt_chars": len(system_prompt),
        "current_input_chars": len(prompt),
        "available_tools": tool_names,
        "active_tools": tool_names,
        "plan_present": bool(plan),
        "memory": {
            "storage_lifetime": "cross-session",
            "selected_into_this_context": bool(memory.strip()),
            "selected_chars": len(memory),
        },
        "execution_state_in_context": execution_state_selected,
        "prior_messages_present": message_count > 0,
    }
