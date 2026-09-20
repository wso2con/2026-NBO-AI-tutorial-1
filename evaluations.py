"""Deterministic outcome and trajectory checks for the loop demo.

The suite exercises the same state transitions and file-backed business
operations used by the live scenarios. It deliberately avoids LLM scoring so
the result is stable on stage. Model-quality evaluation belongs in a larger
offline suite; this panel proves harness behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from budget_demo import stream_budget_investigation
from loop_state import (
    RunStore,
    apply_task_contract,
    grace_threshold_calls,
    record_iteration,
    record_observation,
    record_tool_call,
    validate_response,
    verify_postconditions,
)
from cs_agent_engineered.agent.skill_contracts import load_skill_contract
from mocks.client import CustomerSupportClient, reset_data_files

EXPECTED_BEHAVIORS = {
    "foundations-see-loop": {
        "first_cut": "Exposes the basic model, tool, observation, and stop sequence.",
        "engineered": "Exposes the same basic sequence with harness-owned run state.",
    },
    "tools-refund-history": {
        "first_cut": "Receives a deeply nested legacy envelope with noisy metadata and renamed business fields.",
        "engineered": "Receives a focused refund observation whose fields can directly inform the next model call.",
    },
    "skills-address-change": {
        "first_cut": "Usually handles only the named order because no cross-order address procedure exists.",
        "engineered": "Loads the address-change procedure, inspects related orders, and asks before broader action.",
    },
    "tools-net-refund": {
        "first_cut": "The overloaded write contract encourages a full 90% refund without subtracting the prior 10% credit.",
        "engineered": "Cancels first and refunds the remaining 80%, leaving the order at the 90% policy entitlement.",
    },
    "context-repeat-damage": {
        "first_cut": "Receives the prior payout wrapped in a legacy envelope, where the fact that #1243 is already settled is easy to skip past.",
        "engineered": "Receives a typed observation that states the prior refund as a percentage of the order, alongside the damaged-on-arrival status.",
    },
    "memory-promise-lapse": {
        "first_cut": "Loses the travel deadline and prior promise when conversation history ends.",
        "engineered": "Retrieves the promise after the session boundary and uses it to recognize urgency.",
    },
    "memory-scope-action": {
        "first_cut": "Shared conversation history can carry Alice's order reference into Carol's turn.",
        "engineered": "Keeps Alice's order and memory out of Carol's context and actions.",
    },
    "control-conditional-plan": {
        "first_cut": "Has an overloaded write tool and no discoverable cancellation procedure.",
        "engineered": "Checks shipping state and policy before selecting a safe in-transit remedy.",
    },
    "control-ask-resume": {
        "first_cut": "May ask in prose, but has no typed blocker or durable resume state.",
        "engineered": "Exits ASK with the blocker, original goal, and resume condition preserved.",
    },
    "control-budget-pressure": {
        "first_cut": "Executes ten calls, rejects the eleventh, and exits with a budget error.",
        "engineered": "Pauses at the 90% guard and resumes only the remaining work next turn.",
    },
    "safety-identity-binding": {
        "first_cut": "Customer identity remains a prompt instruction that the model can replace.",
        "engineered": "The harness fills missing identity and rejects any customer ID that differs from Alice's authenticated ID.",
    },
    "recovery-timeout-after-commit": {
        "first_cut": "Retries the unknown write and creates two refunds.",
        "engineered": "Reconciles by operation ID and confirms exactly one refund.",
    },
    "validation-damaged-item": {
        "first_cut": "Can produce a plausible refund answer without checking the damaged-item prerequisites.",
        "engineered": "Withholds the reply unless policy, photo request, and return-label steps are complete; refund-before-photo is rejected.",
    },
    "validation-late-credit": {
        "first_cut": "Can promise a credit after only looking at the order, without policy or refund-history evidence.",
        "engineered": "Releases the reply only after order, policy, prior refunds, and the successful credit are verified in order.",
    },
}


def success_criteria_for(scenario_id: str, _prompt: str = "") -> list[str]:
    """Evaluation expectations only; live agents never receive these IDs."""
    criteria = {
        "control-ask-resume": ["required_input_available", "address_update_observed"],
        "control-budget-pressure": ["multi_step_investigation_complete"],
        "recovery-timeout-after-commit": ["refund_committed_once", "refund_reconciled"],
        "validation-damaged-item": [
            "order_observed", "policy_observed", "photo_evidence_requested",
            "return_label_arranged", "response_produced",
        ],
        "validation-late-credit": [
            "order_observed", "policy_observed", "refund_history_observed",
            "refund_committed", "response_produced",
        ],
        "tools-refund-history": ["order_observed", "refund_history_observed", "response_produced"],
        "skills-address-change": ["procedure_loaded", "related_orders_observed", "response_produced"],
        "tools-net-refund": [
            "order_observed", "policy_observed", "refund_history_observed",
            "order_cancelled", "refund_committed",
        ],
        "control-conditional-plan": ["order_observed", "policy_observed", "refund_committed"],
    }
    return list(criteria.get(scenario_id, ["response_produced"]))


def _apply_skill_contract(run, lab_root: Path, skill_name: str) -> None:
    contract = load_skill_contract(lab_root / "cs_agent_engineered" / "skills", skill_name)
    if contract is None:
        raise AssertionError(f"missing contract for {skill_name}")
    apply_task_contract(run, contract, f"skill:{skill_name}")


def _result(
    scenario_id: str,
    case: str,
    first_cut: bool,
    engineered: bool,
    *,
    outcome_assertions: list[dict[str, Any]],
    trajectory_assertions: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "case": case,
        "kind": "outcome_and_trajectory",
        "first_cut": {"passed": bool(first_cut)},
        "engineered": {"passed": bool(engineered)},
        "expected_behavior": EXPECTED_BEHAVIORS[scenario_id],
        "outcome_assertions": outcome_assertions,
        "trajectory_assertions": trajectory_assertions,
        "evidence": evidence,
    }


def _assertion(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed)}


def run_suite(lab_root: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    # Reseed first: the client is file-backed, so without this the baseline
    # keeps whatever was on disk from an earlier run and the suite silently
    # evaluates stale data after any change to mocks/seeds/.
    reset_data_files("eval_baseline")
    baseline = CustomerSupportClient(agent_id="eval_baseline")
    order = baseline.get_order("1234")

    # 1. Foundations: both implementations expose the basic loop. The
    # engineered version adds stronger decisions later in the suite; the
    # first-cut version is not expected to fail this introductory scenario.
    visible_loop = RunStore().start(
        run_id="eval-visible-loop",
        scenario_id="foundations-see-loop",
        customer_id="cust_001",
        goal="investigate an order",
    )
    visible_loop.success_criteria = ["order_observed"]
    record_iteration(visible_loop)
    record_tool_call(visible_loop, "get_order")
    record_observation(
        visible_loop,
        tool_name="get_order",
        observation=order.model_dump() if order else {"error": "order_not_found"},
        is_error=order is None,
    )
    loop_verified, loop_missing = verify_postconditions(visible_loop)
    results.append(
        _result(
            "foundations-see-loop",
            "See the loop",
            loop_verified,
            loop_verified,
            outcome_assertions=[
                _assertion("A backend observation grounds the answer", loop_verified),
            ],
            trajectory_assertions=[
                _assertion("A model iteration is recorded", visible_loop.iteration_count == 1),
                _assertion("A tool invocation is recorded", visible_loop.tool_call_count == 1),
            ],
            evidence={"run": visible_loop.dump(), "missing_criteria": loop_missing},
        )
    )

    # 2. Tool observations: the first-cut adapter returns a SOAP-shaped legacy
    # envelope. The engineered service returns the same business fact in a
    # compact, action-oriented schema suitable for the next model call.
    legacy_refunds = baseline.get_refund_history_legacy("cust_001")
    focused_refunds = baseline.get_refund_history("cust_001")
    legacy_is_focused = (
        set(legacy_refunds) == {"refunds", "count"}
        and isinstance(legacy_refunds.get("refunds"), list)
    )
    focused_is_actionable = bool(
        focused_refunds
        and all(
            key in focused_refunds[0]
            for key in ("order_id", "amount_usd", "refund_percentage", "reason")
        )
        and any(item.get("order_id") == "1241" for item in focused_refunds)
    )
    results.append(
        _result(
            "tools-refund-history",
            "A useful tool observation",
            legacy_is_focused,
            focused_is_actionable,
            outcome_assertions=[
                _assertion("The prior water-bottle credit is observable", focused_is_actionable),
                _assertion("Refund percentage is a typed field", "refund_percentage" in focused_refunds[0]),
            ],
            trajectory_assertions=[
                _assertion(
                    "Engineered observation avoids the legacy envelope",
                    "RefundEnvelope" not in json.dumps(focused_refunds)
                    and "@xmlns" not in json.dumps(focused_refunds),
                ),
                _assertion("First-cut observation contains adapter metadata", "@xmlns" in legacy_refunds and "RefundEnvelope" in legacy_refunds),
            ],
            evidence={
                "first_cut_top_level_keys": list(legacy_refunds),
                "engineered_observation": {"refunds": focused_refunds, "count": len(focused_refunds)},
            },
        )
    )

    # 3. Skills: the address procedure adds cross-order behavior that is not
    # already obvious from the base prompt.
    address_skill = (
        lab_root
        / "cs_agent_engineered"
        / "skills"
        / "handle-shipping-address-change"
        / "SKILL.md"
    )
    first_address_skill = (
        lab_root
        / "cs_agent_first_cut"
        / "skills"
        / "handle-shipping-address-change"
        / "SKILL.md"
    )
    address_skill_text = (
        address_skill.read_text(encoding="utf-8") if address_skill.exists() else ""
    )
    skill_ok = all(
        phrase in address_skill_text
        for phrase in ("get_customer_orders", "Partition orders by status", "ASK before acting")
    )
    results.append(
        _result(
            "skills-address-change",
            "Address change across open orders",
            first_address_skill.exists(),
            skill_ok,
            outcome_assertions=[
                _assertion("Address-change procedure is discoverable", address_skill.exists()),
            ],
            trajectory_assertions=[
                _assertion("Procedure inspects related orders", "get_customer_orders" in address_skill_text),
                _assertion("Procedure partitions orders by status", "Partition orders by status" in address_skill_text),
                _assertion("Procedure asks before broader action", "ASK before acting" in address_skill_text),
            ],
            evidence={
                "first_cut_skill_present": first_address_skill.exists(),
                "engineered_skill": str(address_skill.relative_to(lab_root)),
            },
        )
    )

    first_budget = RunStore().start(
        run_id="eval-budget-first-cut",
        scenario_id="control-budget-pressure",
        customer_id="cust_001",
        goal="complete a multi-step investigation",
        tool_budget=10,
    )
    list(
        stream_budget_investigation(
            run=first_budget,
            variant="first_cut",
            agent_id="eval_budget_first_cut",
            customer_id="cust_001",
            framed_prompt="investigate safely",
        )
    )
    first_budget_ok = (
        first_budget.status == "paused"
        and first_budget.next_loop_state == "PAUSE"
        and bool(first_budget.resume_condition)
    )

    store = RunStore()
    budgeted = store.start(
        run_id="eval-budget",
        scenario_id="control-budget-pressure",
        customer_id="cust_001",
        goal="complete a multi-step investigation",
        tool_budget=10,
    )
    budgeted.success_criteria = success_criteria_for(
        budgeted.scenario_id, budgeted.goal
    )
    record_iteration(budgeted)
    for _index in range(9):
        record_tool_call(budgeted, "get_order")
    budgeted.next_loop_state = (
        "PAUSE"
        if budgeted.tool_call_count >= grace_threshold_calls(budgeted)
        else "CONTINUE"
    )
    budgeted.resume_condition = (
        "start_next_turn_with_fresh_tool_budget"
        if budgeted.next_loop_state == "PAUSE"
        else None
    )
    pause_ok = (
        budgeted.next_loop_state == "PAUSE"
        and budgeted.tool_call_count < budgeted.max_tool_calls
    )
    results.append(
        _result(
            "control-budget-pressure",
            "Per-turn tool budget",
            first_budget_ok,
            pause_ok,
            outcome_assertions=[
                _assertion(
                    "Run pauses without claiming completion",
                    budgeted.next_loop_state == "PAUSE",
                ),
                _assertion(
                    "Resume condition is explicit",
                    budgeted.resume_condition
                    == "start_next_turn_with_fresh_tool_budget",
                ),
            ],
            trajectory_assertions=[
                _assertion(
                    "Parallel batch remains one model iteration",
                    budgeted.iteration_count == 1,
                ),
                _assertion(
                    "90% guard is reached",
                    budgeted.tool_call_count == grace_threshold_calls(budgeted),
                ),
                _assertion(
                    "One hard-budget call remains",
                    budgeted.dump()["tool_calls_remaining"] == 1,
                ),
            ],
            evidence={
                "first_cut": first_budget.dump(),
                "engineered": budgeted.dump(),
            },
        )
    )

    first_ask_run = store.start(
        run_id="eval-ask-first-cut",
        scenario_id="control-ask-resume",
        customer_id="cust_001",
        goal="update destination",
    )
    first_ask_ok = (
        first_ask_run.next_loop_state == "ASK"
        and first_ask_run.blocker == "new_address_unavailable"
        and bool(first_ask_run.pending_request)
    )

    ask_run = store.start(
        run_id="eval-ask",
        scenario_id="control-ask-resume",
        customer_id="cust_001",
        goal="update destination",
    )
    ask_run.success_criteria = success_criteria_for(ask_run.scenario_id, ask_run.goal)
    ask_run.blocker = "new_address_unavailable"
    ask_run.next_loop_state = "ASK"
    ask_run.pending_request = (
        "Change the shipping address for order #1240 to my new address."
    )
    preserved = store.get("eval-ask") is ask_run
    ask_run.blocker = None
    ask_run.verified_criteria["required_input_available"] = True
    ask_run.progress["new_address"] = "provided"
    ask_run.next_loop_state = "CONTINUE"
    results.append(
        _result(
            "control-ask-resume",
            "Missing input and resume",
            first_ask_ok,
            preserved and ask_run.verified_criteria["required_input_available"],
            outcome_assertions=[
                _assertion("Missing input produces ASK", preserved),
                _assertion("New input clears the blocker", ask_run.blocker is None),
            ],
            trajectory_assertions=[
                _assertion(
                    "The same run survives the pause", store.get("eval-ask") is ask_run
                ),
                _assertion(
                    "The original request remains available",
                    bool(ask_run.pending_request),
                ),
            ],
            evidence={"first_cut": first_ask_run.dump(), "engineered": ask_run.dump()},
        )
    )

    # 4. Tool contracts: execute the original net-refund arithmetic against
    # isolated ledgers. The seed already contains a 10% credit for #1241.
    for agent_id in ("eval_tools_first_cut", "eval_tools_engineered"):
        reset_data_files(agent_id)
    first_tools = CustomerSupportClient(agent_id="eval_tools_first_cut")
    engineered_tools = CustomerSupportClient(agent_id="eval_tools_engineered")
    first_tools.cancel_order("1241", "customer_requested", "eval_tools_first_cut")
    first_tools.issue_refund(
        "1241",
        "cust_001",
        90.0,
        "full_cancellation_percentage",
        "eval_tools_first_cut",
        refund_percentage=0.90,
    )
    engineered_tools.cancel_order(
        "1241", "customer_requested", "eval_tools_engineered"
    )
    engineered_tools.issue_refund(
        "1241",
        "cust_001",
        80.0,
        "cancel_net_of_prior",
        "eval_tools_engineered",
        refund_percentage=0.80,
    )

    def refund_total(client: CustomerSupportClient) -> float:
        return sum(
            float(item.get("refund_percentage") or 0)
            for item in client.get_refund_history("cust_001")
            if item.get("order_id") == "1241"
        )

    first_total = refund_total(first_tools)
    engineered_total = refund_total(engineered_tools)
    engineered_entries = [
        entry
        for entry in engineered_tools.ledger_entries()
        if entry.get("actor_agent_id") == "eval_tools_engineered"
        and entry.get("order_id") == "1241"
    ]
    cancel_before_refund = [entry.get("kind") for entry in engineered_entries] == [
        "cancel",
        "refund",
    ]
    first_verified_ok = abs(first_total - 0.90) < 1e-9
    verified_ok = (
        abs(engineered_total - 0.90) < 1e-9
        and cancel_before_refund
        and engineered_tools.get_order("1241").status == "cancelled"
    )
    results.append(
        _result(
            "tools-net-refund",
            "Cancel and calculate the net refund",
            first_verified_ok,
            verified_ok,
            outcome_assertions=[
                _assertion("Cancellation entitlement totals 90%", abs(engineered_total - 0.90) < 1e-9),
                _assertion("New refund is the remaining 80%", any(entry.get("refund_percentage") == 0.80 for entry in engineered_entries)),
            ],
            trajectory_assertions=[
                _assertion("Cancellation precedes refund", cancel_before_refund),
                _assertion("Order ends cancelled", engineered_tools.get_order("1241").status == "cancelled"),
            ],
            evidence={
                "prior_credit_percentage": 0.10,
                "policy_cancellation_percentage": 0.90,
                "first_cut_new_refund_percentage": 0.90,
                "first_cut_total_refunded_percentage": first_total,
                "engineered_new_refund_percentage": 0.80,
                "engineered_total_refunded_percentage": engineered_total,
                "engineered_write_order": [entry.get("kind") for entry in engineered_entries],
            },
        )
    )

    # Context: a returning customer asking again about an order the ledger has
    # already settled. Both loops can reach every fact they need through their
    # own tools; what differs is the shape of the observation each one gets
    # back. Deterministic checks only - the live behaviour is the demo.
    damaged_order = baseline.get_order("1243")
    alice_refunds = baseline.get_refund_history("cust_001")
    refunds_on_1243 = [e for e in alice_refunds if e.get("order_id") == "1243"]
    already_refunded_pct = sum(
        float(e.get("refund_percentage") or 0.0) for e in refunds_on_1243
    )
    settled_in_full = abs(already_refunded_pct - 1.0) < 1e-9
    order_is_damaged = bool(
        damaged_order
        and damaged_order.damaged
        and damaged_order.status == "delivered_damaged"
    )
    # The engineered observation types the prior payout as a percentage field;
    # the first-cut envelope buries the same fact under adapter metadata.
    legacy_alice = baseline.get_refund_history_legacy("cust_001")
    engineered_exposes_percentage = bool(
        refunds_on_1243 and "refund_percentage" in refunds_on_1243[0]
    )
    first_cut_buries_it = "@xmlns" in legacy_alice and "RefundEnvelope" in legacy_alice
    engineered_context_ok = bool(
        settled_in_full and order_is_damaged and engineered_exposes_percentage
    )
    results.append(
        _result(
            "context-repeat-damage",
            "A returning customer with history",
            not first_cut_buries_it,
            engineered_context_ok,
            outcome_assertions=[
                _assertion("Order #1243 is flagged damaged on arrival", order_is_damaged),
                _assertion("The ledger already settles #1243 in full", settled_in_full),
            ],
            trajectory_assertions=[
                _assertion(
                    "Engineered observation types the prior payout as refund_percentage",
                    engineered_exposes_percentage,
                ),
                _assertion(
                    "First-cut observation wraps the same fact in adapter metadata",
                    first_cut_buries_it,
                ),
            ],
            evidence={
                "order": "1243",
                "status": damaged_order.status if damaged_order else None,
                "already_refunded_percentage": already_refunded_pct,
                "first_cut_top_level_keys": list(legacy_alice),
            },
        )
    )

    # 5 and 6. Episodic memory: exercise the real file-backed memory helpers
    # in an isolated temporary directory so evaluation never changes demo data.
    from cs_agent_engineered.agent import memory as episodic_memory

    original_memory_dir = episodic_memory.MEMORY_DIR
    try:
        with TemporaryDirectory(prefix="loop-eval-memory-") as temp_dir:
            episodic_memory.MEMORY_DIR = Path(temp_dir)
            episodic_memory.append("cust_001", "Open promise for Alice")
            alice_before_session_end = episodic_memory.load("cust_001")
            alice_after_session_end = episodic_memory.load("cust_001")
            carol_memory = episodic_memory.load("cust_003")
    finally:
        episodic_memory.MEMORY_DIR = original_memory_dir

    continuity_ok = (
        "Open promise for Alice" in alice_before_session_end
        and alice_after_session_end == alice_before_session_end
    )
    first_cut_memory_dir = lab_root / "cs_agent_first_cut" / "memory" / "episodic"
    first_continuity_ok = first_cut_memory_dir.exists()
    results.append(
        _result(
            "memory-promise-lapse",
            "A promise survives the session",
            first_continuity_ok,
            continuity_ok,
            outcome_assertions=[
                _assertion("Alice's note survives a session boundary", continuity_ok),
            ],
            trajectory_assertions=[
                _assertion("Memory is stored outside conversation history", alice_after_session_end == alice_before_session_end),
            ],
            evidence={
                "first_cut_durable_memory_present": first_continuity_ok,
                "stored_for": "cust_001",
                "selected_text_present": continuity_ok,
            },
        )
    )
    scope_ok = "Open promise for Alice" not in carol_memory and not carol_memory.strip()
    first_main_source = (lab_root / "cs_agent_first_cut" / "main.py").read_text(encoding="utf-8")
    first_scope_ok = "_AGENTS: dict[str, Agent]" in first_main_source
    results.append(
        _result(
            "memory-scope-action",
            "Different customer, same chat",
            first_scope_ok,
            scope_ok,
            outcome_assertions=[
                _assertion("Carol does not receive Alice's memory", scope_ok),
            ],
            trajectory_assertions=[
                _assertion("Memory is keyed by customer", scope_ok),
            ],
            evidence={
                "first_cut_per_customer_agent_cache": first_scope_ok,
                "alice_has_note": continuity_ok,
                "carol_memory_chars": len(carol_memory),
            },
        )
    )

    # 7. Conditional action: verify the shipped procedure encodes the safe
    # branch for an already-shipped order. Model adherence remains a live-run
    # observation and is deliberately not asserted by this deterministic suite.
    cancel_skill = lab_root / "cs_agent_engineered" / "skills" / "handle-cancellation" / "SKILL.md"
    first_cancel_skill = lab_root / "cs_agent_first_cut" / "skills" / "handle-cancellation" / "SKILL.md"
    cancel_text = cancel_skill.read_text(encoding="utf-8") if cancel_skill.exists() else ""
    conditional_ok = bool(
        order
        and order.status in {"in_transit", "in_transit_delayed"}
        and "already shipped" in cancel_text
        and "cancellation is not allowed" in cancel_text
        and "search_policy_kb" in cancel_text
    )
    first_tools_text = (lab_root / "cs_agent_first_cut" / "tools.py").read_text(encoding="utf-8")
    first_conditional_ok = (
        "def modify_order" not in first_tools_text and first_cancel_skill.exists()
    )
    results.append(
        _result(
            "control-conditional-plan",
            "Conditional cancellation / refund",
            first_conditional_ok,
            conditional_ok,
            outcome_assertions=[
                _assertion("Order is already in transit", bool(order and order.status in {"in_transit", "in_transit_delayed"})),
                _assertion("Procedure forbids cancellation after shipment", "cancellation is not allowed" in cancel_text),
            ],
            trajectory_assertions=[
                _assertion("Procedure checks shipping state before the write", "Check status" in cancel_text),
                _assertion("Procedure requires policy evidence", "search_policy_kb" in cancel_text),
            ],
            evidence={
                "first_cut_overloaded_modify_order_present": "def modify_order" in first_tools_text,
                "order_status": getattr(order, "status", None),
                "procedure": str(cancel_skill.relative_to(lab_root)),
            },
        )
    )

    # 10. Trusted identity: verify that only the engineered agent installs a
    # hook that overwrites model-supplied customer IDs.
    first_agent_source = (lab_root / "cs_agent_first_cut" / "agent.py").read_text(
        encoding="utf-8"
    )
    engineered_core_source = (
        lab_root / "cs_agent_engineered" / "agent" / "core.py"
    ).read_text(encoding="utf-8")
    hooks_source = (lab_root / "cs_agent_engineered" / "agent" / "hooks.py").read_text(
        encoding="utf-8"
    )
    first_identity_bound = "hooks_.append(CustomerIdBindingHook" in first_agent_source
    engineered_identity_bound = (
        "hooks_.append(CustomerIdBindingHook" in engineered_core_source
        and "inputs[\"customer_id\"] = self.customer_id" in hooks_source
        and '"error": "unauthorized"' in hooks_source
    )
    results.append(
        _result(
            "safety-identity-binding",
            "Identity switch attempt",
            first_identity_bound,
            engineered_identity_bound,
            outcome_assertions=[
                _assertion(
                    "Engineered agent installs trusted identity binding",
                    engineered_identity_bound,
                ),
            ],
            trajectory_assertions=[
                _assertion(
                    "Mismatched customer ID is rejected before tool execution",
                    '"error": "unauthorized"' in hooks_source,
                ),
            ],
            evidence={
                "first_cut_identity_is_prompt_text": not first_identity_bound,
                "engineered_binding_hook_present": engineered_identity_bound,
            },
        )
    )

    for agent_id in ("eval_first_cut", "eval_engineered"):
        reset_data_files(agent_id)
    first = CustomerSupportClient(agent_id="eval_first_cut")
    engineered = CustomerSupportClient(agent_id="eval_engineered")
    amount = 8.95
    first.issue_refund(
        "1234", "cust_001", amount, "initial", "eval_first_cut", refund_percentage=0.10
    )
    first.issue_refund(
        "1234",
        "cust_001",
        amount,
        "blind_retry",
        "eval_first_cut",
        refund_percentage=0.10,
    )
    operation_id = "eval-refund-1234"
    engineered.issue_refund(
        "1234",
        "cust_001",
        amount,
        "initial",
        "eval_engineered",
        refund_percentage=0.10,
        operation_id=operation_id,
    )
    engineered.issue_refund(
        "1234",
        "cust_001",
        amount,
        "retry",
        "eval_engineered",
        refund_percentage=0.10,
        operation_id=operation_id,
    )
    # The canonical seed intentionally contains historical refunds. Count only
    # writes created by this evaluation, otherwise a clean reset still appears
    # to create duplicate recovery operations.
    first_refunds = [
        entry
        for entry in first.ledger_entries()
        if entry.get("kind") == "refund"
        and entry.get("actor_agent_id") == "eval_first_cut"
    ]
    engineered_refunds = [
        entry
        for entry in engineered.ledger_entries()
        if entry.get("kind") == "refund"
        and entry.get("actor_agent_id") == "eval_engineered"
    ]
    reconciled = engineered.get_refund_by_operation(operation_id)
    recovery_ok = len(engineered_refunds) == 1 and reconciled is not None
    results.append(
        _result(
            "recovery-timeout-after-commit",
            "Timeout after refund commit",
            len(first_refunds) == 1,
            recovery_ok,
            outcome_assertions=[
                _assertion("Exactly one refund exists", len(engineered_refunds) == 1),
                _assertion("Committed operation can be found", reconciled is not None),
            ],
            trajectory_assertions=[
                _assertion(
                    "Retry reused the operation ID",
                    all(
                        entry.get("operation_id") == operation_id
                        for entry in engineered_refunds
                    ),
                ),
                _assertion(
                    "Blind retry demonstrates duplicate risk", len(first_refunds) == 2
                ),
            ],
            evidence={
                "first_cut_refund_count": len(first_refunds),
                "engineered_refund_count": len(engineered_refunds),
                "operation_id": operation_id,
            },
        )
    )

    # 12. Validation: a plausible damaged-item answer is unsafe when the
    # required photo and return-label procedure have not been completed.
    damaged_first = RunStore().start(
        run_id="eval-validation-damaged-first",
        scenario_id="validation-damaged-item",
        customer_id="cust_001",
        goal="resolve damaged coat",
    )
    _apply_skill_contract(damaged_first, lab_root, "handle-damaged-item")
    record_tool_call(damaged_first, "get_order", {"order_id": "1239"})
    record_observation(
        damaged_first,
        tool_name="get_order",
        observation={"order_id": "1239", "status": "delivered_damaged"},
    )
    record_tool_call(
        damaged_first,
        "issue_refund",
        {"order_id": "1239", "refund_percentage": 1.0},
    )
    record_observation(
        damaged_first,
        tool_name="issue_refund",
        observation={"ok": True, "ref": "unsafe-refund"},
    )
    damaged_first_validation = validate_response(
        damaged_first, "I issued a full refund for the damaged coat."
    )

    damaged_engineered = RunStore().start(
        run_id="eval-validation-damaged-engineered",
        scenario_id="validation-damaged-item",
        customer_id="cust_001",
        goal="resolve damaged coat",
    )
    _apply_skill_contract(damaged_engineered, lab_root, "handle-damaged-item")
    damaged_engineered.verified_criteria["procedure_loaded"] = True
    record_tool_call(damaged_engineered, "get_order", {"order_id": "1239"})
    record_observation(
        damaged_engineered,
        tool_name="get_order",
        observation={"order_id": "1239", "status": "delivered_damaged"},
    )
    record_tool_call(
        damaged_engineered,
        "search_policy_kb",
        {"query": "damaged item photo evidence return label"},
    )
    record_observation(
        damaged_engineered,
        tool_name="search_policy_kb",
        observation={"policy": "damaged_item"},
    )
    record_tool_call(
        damaged_engineered,
        "escalate_to_human",
        {"reason": "Send the required return label for damaged order #1239"},
    )
    record_observation(
        damaged_engineered,
        tool_name="escalate_to_human",
        observation={"ok": True, "ticket_id": "TICKET-LABEL"},
    )
    damaged_engineered_validation = validate_response(
        damaged_engineered,
        "I arranged the return label. Please upload a clear photo of the damaged area; the refund can be issued after that evidence is received.",
    )
    results.append(
        _result(
            "validation-damaged-item",
            "Damaged item release gate",
            damaged_first_validation["passed"],
            damaged_engineered_validation["passed"],
            outcome_assertions=[
                _assertion(
                    "Correct reply asks for photo evidence",
                    damaged_engineered.verified_criteria.get("photo_evidence_requested", False),
                ),
                _assertion(
                    "Return-label step is arranged",
                    damaged_engineered.verified_criteria.get("return_label_arranged", False),
                ),
            ],
            trajectory_assertions=[
                _assertion(
                    "Unsafe refund-before-photo is rejected",
                    "refund_was_attempted_without_photo_evidence"
                    in damaged_first_validation["violations"],
                ),
                _assertion(
                    "Engineered reply passes the pre-release gate",
                    damaged_engineered_validation["passed"],
                ),
            ],
            evidence={
                "first_cut_validation": damaged_first_validation,
                "engineered_validation": damaged_engineered_validation,
            },
        )
    )

    # 13. Validation: a late-delivery credit requires all prerequisite reads
    # before the write, not merely a convincing final sentence.
    late_first = RunStore().start(
        run_id="eval-validation-credit-first",
        scenario_id="validation-late-credit",
        customer_id="cust_001",
        goal="apply late credit",
    )
    _apply_skill_contract(late_first, lab_root, "handle-refund")
    record_tool_call(late_first, "get_order", {"order_id": "1234"})
    record_observation(
        late_first,
        tool_name="get_order",
        observation={"order_id": "1234", "status": "in_transit_delayed"},
    )
    record_tool_call(
        late_first,
        "issue_refund",
        {"order_id": "1234", "refund_percentage": 0.10},
    )
    record_observation(
        late_first,
        tool_name="issue_refund",
        observation={"ok": True, "ref": "credit-without-checks"},
    )
    late_first_validation = validate_response(
        late_first, "I applied your late-delivery credit."
    )

    late_engineered = RunStore().start(
        run_id="eval-validation-credit-engineered",
        scenario_id="validation-late-credit",
        customer_id="cust_001",
        goal="apply late credit",
    )
    _apply_skill_contract(late_engineered, lab_root, "handle-refund")
    late_engineered.verified_criteria["procedure_loaded"] = True
    for tool_name, args, observation in (
        ("get_order", {"order_id": "1234"}, {"status": "in_transit_delayed"}),
        ("search_policy_kb", {"query": "shipping delay credit"}, {"percentage": 0.10}),
        ("get_refund_history", {"customer_id": "cust_001"}, []),
        (
            "issue_refund",
            {"order_id": "1234", "refund_percentage": 0.10},
            {"ok": True, "ref": "validated-credit"},
        ),
    ):
        record_tool_call(late_engineered, tool_name, args)
        record_observation(
            late_engineered,
            tool_name=tool_name,
            observation=observation,
        )
    late_engineered_validation = validate_response(
        late_engineered, "I verified the delay and prior refunds, then applied the 10% late-delivery credit."
    )
    results.append(
        _result(
            "validation-late-credit",
            "Late-credit completeness gate",
            late_first_validation["passed"],
            late_engineered_validation["passed"],
            outcome_assertions=[
                _assertion(
                    "Validated credit write is observed",
                    late_engineered.verified_criteria.get("refund_committed", False),
                ),
            ],
            trajectory_assertions=[
                _assertion(
                    "Policy precedes the refund",
                    not late_engineered_validation["violations"],
                ),
                _assertion(
                    "Incomplete first-cut trajectory is rejected",
                    not late_first_validation["passed"],
                ),
            ],
            evidence={
                "first_cut_validation": late_first_validation,
                "engineered_validation": late_engineered_validation,
            },
        )
    )

    scenario_order = {
        scenario_id: index
        for index, scenario_id in enumerate(
            (
                "foundations-see-loop",
                "context-repeat-damage",
                "tools-refund-history",
                "tools-net-refund",
                "skills-address-change",
                "memory-promise-lapse",
                "memory-scope-action",
                "control-conditional-plan",
                "safety-identity-binding",
                "control-ask-resume",
                "control-budget-pressure",
                "recovery-timeout-after-commit",
                "validation-damaged-item",
                "validation-late-credit",
            )
        )
    }
    results.sort(key=lambda item: scenario_order[item["scenario_id"]])

    return {
        "results": results,
        "summary": {
            "first_cut": sum(1 for item in results if item["first_cut"]["passed"]),
            "engineered": sum(1 for item in results if item["engineered"]["passed"]),
            "total": len(results),
        },
    }
