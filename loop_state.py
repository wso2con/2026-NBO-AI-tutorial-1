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
    verified_criteria: dict[str, bool] = field(default_factory=dict)
    iteration_count: int = 0
    tool_call_count: int = 0
    max_iterations: int = 6
    max_tool_calls: int = 12
    budget_warning_ratio: float = 0.9
    resume_condition: str | None = None
    pending_request: str | None = None
    exit_reason: str | None = None
    event_count: int = 0
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    validation_attempts: int = 0
    validation_status: str = "pending"
    contract_source: str | None = None
    loaded_skills: list[str] = field(default_factory=list)
    ordering_constraints: list[dict[str, str]] = field(default_factory=list)
    action_preconditions: list[dict[str, str]] = field(default_factory=list)

    def dump(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload.get("scenario_id") is None:
            payload.pop("scenario_id", None)
        payload["tool_budget_used_percent"] = (
            round(100 * self.tool_call_count / self.max_tool_calls)
            if self.max_tool_calls
            else 100
        )
        payload["tool_calls_remaining"] = max(
            0, self.max_tool_calls - self.tool_call_count
        )
        payload["grace_threshold_calls"] = grace_threshold_calls(self)
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
        tool_budget: int | None = None,
        scenario_id: str | None = None,
    ) -> RunState:
        key = run_id or str(uuid4())
        with self._lock:
            run = self._runs.get(key)
            if run is None:
                run = RunState(key, scenario_id, customer_id, goal)
                self._runs[key] = run
            else:
                run.status = "running"
                run.next_loop_state = "CONTINUE"
                run.exit_reason = None
            run.iteration_count = 0
            run.tool_call_count = 0
            run.max_tool_calls = max(1, min(int(tool_budget or 12), 50))
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
    if not is_error:
        run.verified_criteria["useful_observation"] = True
        if tool_name in {"get_order", "get_customer_orders"}:
            run.verified_criteria["order_observed"] = True
        if tool_name in {"lookup_customer", "get_customer_verified"}:
            run.verified_criteria["customer_observed"] = True
        if tool_name == "get_customer_orders":
            run.verified_criteria["related_orders_observed"] = True
        if tool_name == "get_refund_history":
            run.verified_criteria["refund_history_observed"] = True
        if tool_name == "get_open_tickets":
            run.verified_criteria["tickets_observed"] = True
        if tool_name in {"search_policy_kb", "search_kb"}:
            run.verified_criteria["policy_observed"] = True
        if tool_name in {"issue_refund", "modify_order"}:
            run.verified_criteria["refund_committed"] = True
        if tool_name == "cancel_order":
            run.verified_criteria["order_cancelled"] = True
        if tool_name in {"update_shipping_address"}:
            run.verified_criteria["address_update_observed"] = True
        if tool_name in {"escalate_to_human", "escalate"}:
            run.verified_criteria["ticket_opened"] = True
            matching_call = next(
                (
                    item
                    for item in reversed(run.tool_history)
                    if item["name"] == tool_name and item["result"] is observation
                ),
                None,
            )
            reason = str((matching_call or {}).get("args", {}).get("reason", ""))
            if "return label" in reason.lower():
                run.verified_criteria["return_label_arranged"] = True


def grace_threshold_calls(run: RunState) -> int:
    """Whole-call threshold corresponding to the configured warning ratio."""
    return max(1, floor(run.max_tool_calls * run.budget_warning_ratio))


def verify_postconditions(run: RunState) -> tuple[bool, list[str]]:
    missing = [
        criterion
        for criterion in run.success_criteria
        if not run.verified_criteria.get(criterion, False)
    ]
    return not missing, missing


def _tool_positions(run: RunState, name: str) -> list[int]:
    return [
        index
        for index, item in enumerate(run.tool_history)
        if item.get("name") == name and not item.get("is_error")
    ]


def record_response_evidence(run: RunState, reply: str) -> None:
    """Record response-level evidence used by the pre-release validator.

    The checks are deliberately narrow and contract-owned. They do not try to
    grade arbitrary prose; they recognize only observable commitments supplied
    by a Skill that the model actually loaded for this task.
    """
    text = reply.strip().lower()
    run.verified_criteria["response_produced"] = bool(text)
    if "photo_evidence_requested" in run.success_criteria:
        asks_for_photo = "photo" in text and any(
            phrase in text
            for phrase in ("send", "share", "provide", "upload", "need")
        )
        run.verified_criteria["photo_evidence_requested"] = asks_for_photo


def validate_response(run: RunState, reply: str) -> dict[str, Any]:
    """Validate the proposed customer reply before releasing it.

    This gate combines required postconditions with a few deterministic
    trajectory invariants. It is intentionally not another LLM judge: every
    failure points to missing evidence or an observable action ordering.
    """
    record_response_evidence(run, reply)
    complete, missing = verify_postconditions(run)
    violations: list[str] = []

    for constraint in run.ordering_constraints:
        before_positions = _tool_positions(run, constraint["before"])
        after_positions = _tool_positions(run, constraint["after"])
        if after_positions and (
            not before_positions or min(before_positions) > min(after_positions)
        ):
            violations.append(constraint["violation"])
    for precondition in run.action_preconditions:
        if _tool_positions(run, precondition["tool"]) and not run.verified_criteria.get(
            precondition["requires"], False
        ):
            violations.append(precondition["violation"])

    passed = complete and not violations
    run.validation_status = "passed" if passed else "failed"
    return {
        "passed": passed,
        "missing_criteria": missing,
        "violations": violations,
        "checked_reply_chars": len(reply.strip()),
        "tool_calls_checked": len(run.tool_history),
    }


def validation_feedback(result: dict[str, Any]) -> str:
    issues = [
        *(f"missing:{item}" for item in result.get("missing_criteria", [])),
        *(f"violation:{item}" for item in result.get("violations", [])),
    ]
    return ", ".join(issues) or "validation_failed"


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
