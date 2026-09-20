"""Deterministic, read-only investigation used by the budget demo."""

from __future__ import annotations

from typing import Any, Literal

from loop_state import (
    RunState,
    event as loop_event,
    grace_threshold_calls,
    record_iteration,
    record_observation,
    record_tool_call,
    verify_postconditions,
)
from mocks.client import CustomerSupportClient
from policies.search import search

Variant = Literal["first_cut", "engineered"]


def stream_budget_investigation(
    *,
    run: RunState,
    variant: Variant,
    agent_id: str,
    customer_id: str,
    framed_prompt: str,
):
    """Yield one normal SSE trajectory under the configured per-turn budget."""
    import json

    steps = investigation_steps(
        variant=variant,
        agent_id=agent_id,
        customer_id=customer_id,
    )
    run.success_criteria = [f"step:{step['key']}" for step in steps]
    yield {"event": "user_message", "data": json.dumps({"content": framed_prompt})}

    pending_steps = [
        step
        for step in steps
        if variant == "first_cut" or run.progress.get(f"step:{step['key']}") != "done"
    ]
    if pending_steps:
        # One model round may select many independent tools in parallel. The
        # executor still meters every invocation separately, so parallelism
        # cannot bypass the per-turn tool-call budget.
        record_iteration(run)
        yield loop_event(
            "action_selection",
            run,
            f"Selected a batch containing {len(pending_steps)} tool calls",
            selected_actions=[step["name"] for step in pending_steps],
            execution_mode="parallel_eligible_batch",
        )
    for index, step in enumerate(pending_steps):
        if run.tool_call_count >= run.max_tool_calls:
            run.status = "error"
            run.next_loop_state = "BUDGET_EXHAUSTED"
            run.exit_reason = "TOOL_BUDGET_EXHAUSTED"
            yield loop_event(
                "loop_decision",
                run,
                "The next tool call exceeds the per-turn budget",
                decision="BUDGET_EXHAUSTED",
                next_action=step["name"],
            )
            yield loop_event(
                "completion",
                run,
                "Investigation terminated with incomplete work",
                exit_reason=run.exit_reason,
                state=run.dump(),
            )
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": (
                            f"Tool-call budget exhausted at {run.tool_call_count}/"
                            f"{run.max_tool_calls}; next action {step['name']} was not executed."
                        )
                    }
                ),
            }
            return

        record_tool_call(run, step["name"], step["args"])
        tool_use_id = f"budget-{run.run_id}-{run.tool_call_count}"
        yield {
            "event": "tool_call",
            "data": json.dumps(
                {
                    "tool_use_id": tool_use_id,
                    "name": step["name"],
                    "args": step["args"],
                    "args_summary": _args_summary(step["args"]),
                }
            ),
        }
        is_error = (
            isinstance(step["observation"], dict) and "error" in step["observation"]
        )
        yield {
            "event": "tool_result",
            "data": json.dumps(
                {
                    "tool_use_id": tool_use_id,
                    "name": step["name"],
                    "result": step["observation"],
                    "result_summary": step["summary"],
                    "is_error": is_error,
                }
            ),
        }
        record_observation(
            run,
            tool_name=step["name"],
            observation=step["observation"],
            is_error=is_error,
        )
        run.progress[f"step:{step['key']}"] = "done" if not is_error else "error"
        run.verified_criteria[f"step:{step['key']}"] = not is_error
        remaining = pending_steps[index + 1 :]
        run.progress["next_step"] = remaining[0]["key"] if remaining else "none"
        yield loop_event(
            "state_transition",
            run,
            f"Completed {run.tool_call_count} of {run.max_tool_calls} allowed tool calls",
            state=run.dump(),
        )

        if (
            variant == "engineered"
            and remaining
            and run.tool_call_count >= grace_threshold_calls(run)
        ):
            run.status = "paused"
            run.next_loop_state = "PAUSE"
            run.exit_reason = "PAUSED_AT_BUDGET_GUARD"
            run.resume_condition = "start_next_turn_with_fresh_tool_budget"
            run.pending_request = framed_prompt
            yield loop_event(
                "loop_decision",
                run,
                "Pause gracefully at the 90% tool-budget guard",
                decision="PAUSE",
                next_action=remaining[0]["name"],
                resume_condition=run.resume_condition,
            )
            reply = (
                f"I completed {run.tool_call_count} investigation steps and paused before "
                f"starting {remaining[0]['summary']}. The completed evidence is preserved; "
                "continue in a new turn with a fresh budget."
            )
            run.validation_attempts += 1
            run.validation_status = "passed"
            yield loop_event(
                "validation",
                run,
                "Pause response matches preserved progress and resume condition",
                validation={
                    "passed": True,
                    "decision": "PAUSE",
                    "completed_steps": run.tool_call_count,
                    "resume_condition": run.resume_condition,
                },
            )
            yield {"event": "text_delta", "data": json.dumps({"delta": reply})}
            yield loop_event(
                "completion",
                run,
                "Run paused with completed work preserved",
                exit_reason=run.exit_reason,
                state=run.dump(),
            )
            yield {"event": "done", "data": json.dumps({"final_reply": reply})}
            return

        if remaining:
            yield loop_event(
                "loop_decision",
                run,
                "Continue to the next required investigation step",
                decision="CONTINUE",
                next_action=remaining[0]["name"],
            )

    verified, missing = verify_postconditions(run)
    run.verified_criteria["multi_step_investigation_complete"] = verified
    run.status = "complete" if verified else "stopped"
    run.next_loop_state = "COMPLETE" if verified else "STOP"
    run.exit_reason = "GOAL_VERIFIED" if verified else "POSTCONDITION_UNMET"
    run.resume_condition = None
    run.pending_request = None
    reply = (
        "The investigation is complete. Order #1234 belongs to the verified customer, "
        "is four days late, and remains in transit. The safe remedy is the $10 store "
        "credit allowed by the shipping-delay policy; do not cancel or issue a cash "
        "refund while delivery is still expected."
        if verified
        else f"The investigation stopped because these checks lack evidence: {', '.join(missing)}."
    )
    yield loop_event(
        "loop_decision",
        run,
        "All required investigation steps have observable results",
        decision=run.next_loop_state,
        missing_criteria=missing,
    )
    if variant == "engineered":
        run.validation_attempts += 1
        run.validation_status = "passed" if verified else "failed"
        yield loop_event(
            "validation",
            run,
            "Investigation result passed the release gate"
            if verified
            else "Investigation result remains incomplete",
            validation={"passed": verified, "missing_criteria": missing},
        )
    yield {"event": "text_delta", "data": json.dumps({"delta": reply})}
    yield loop_event(
        "completion",
        run,
        "Goal verified within budget"
        if verified
        else "Required evidence remains missing",
        exit_reason=run.exit_reason,
        state=run.dump(),
    )
    yield {"event": "done", "data": json.dumps({"final_reply": reply})}


def _args_summary(args: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in args.items())


def _order(client: CustomerSupportClient, order_id: str) -> dict[str, Any]:
    value = client.get_order(order_id)
    return (
        value.model_dump()
        if value
        else {"error": "order_not_found", "order_id": order_id}
    )


def investigation_steps(
    *,
    variant: Variant,
    agent_id: str,
    customer_id: str,
) -> list[dict[str, Any]]:
    """Return eleven useful reads so a presenter can create budget pressure."""
    client = CustomerSupportClient(agent_id=agent_id)
    customer = client.get_customer(customer_id)
    customer_observation: Any
    customer_tool: str
    if variant == "first_cut":
        customer_tool = "get_customer_verified"
        customer_observation = customer.verified if customer else False
        refunds = client.get_refund_history_legacy(customer_id)
        tickets = client.get_open_tickets_legacy(customer_id)
    else:
        customer_tool = "lookup_customer"
        customer_observation = (
            customer.model_dump() if customer else {"error": "customer_not_found"}
        )
        refunds = client.get_refund_history(customer_id)
        tickets = client.get_open_tickets(customer_id)

    customer_orders = [
        item.model_dump() for item in client.get_customer_orders(customer_id)
    ]
    return [
        _step(
            "customer",
            customer_tool,
            {"customer_id": customer_id},
            customer_observation,
            "customer identity and verification",
        ),
        _step(
            "target_order",
            "get_order",
            {"customer_id": customer_id, "order_id": "1234"},
            _order(client, "1234"),
            "target order status",
        ),
        _step(
            "related_orders",
            "get_customer_orders",
            {"customer_id": customer_id},
            customer_orders,
            f"{len(customer_orders)} customer orders",
        ),
        _step(
            "refund_history",
            "get_refund_history",
            {"customer_id": customer_id},
            refunds,
            "refund history",
        ),
        _step(
            "open_tickets",
            "get_open_tickets",
            {"customer_id": customer_id},
            tickets,
            "open support tickets",
        ),
        _step(
            "shipping_policy",
            "search_policy_kb",
            {"query": "shipping delay credit"},
            search("shipping delay credit"),
            "shipping-delay policy",
        ),
        _step(
            "delivered_order",
            "get_order",
            {"customer_id": customer_id, "order_id": "1238"},
            _order(client, "1238"),
            "delivered-order comparison",
        ),
        _step(
            "damaged_order",
            "get_order",
            {"customer_id": customer_id, "order_id": "1239"},
            _order(client, "1239"),
            "damaged-order comparison",
        ),
        _step(
            "refund_policy",
            "search_policy_kb",
            {"query": "refund authority limit"},
            search("refund authority limit"),
            "refund-authority policy",
        ),
        _step(
            "placed_order",
            "get_order",
            {"customer_id": customer_id, "order_id": "1240"},
            _order(client, "1240"),
            "placed-order comparison",
        ),
        _step(
            "preparing_order",
            "get_order",
            {"customer_id": customer_id, "order_id": "1241"},
            _order(client, "1241"),
            "preparing-order comparison",
        ),
    ]


def _step(
    key: str,
    name: str,
    args: dict[str, Any],
    observation: Any,
    summary: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "args": args,
        "observation": observation,
        "summary": summary,
    }
