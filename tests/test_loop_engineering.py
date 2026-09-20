from pathlib import Path
import unittest

from budget_demo import stream_budget_investigation
from evaluations import run_suite
from loop_state import (
    RunStore,
    apply_task_contract,
    context_snapshot,
    grace_threshold_calls,
    record_iteration,
    record_observation,
    record_tool_call,
    validate_response,
    verify_postconditions,
)
from mocks.client import CustomerSupportClient, reset_data_files
from cs_agent_engineered.agent.skill_contracts import load_skill_contract


ROOT = Path(__file__).resolve().parents[1]


class LoopStateTests(unittest.TestCase):
    def test_run_state_survives_ask_resume_boundary(self) -> None:
        store = RunStore()
        run = store.start(
            run_id="run-1",
            scenario_id="control-ask-resume",
            customer_id="cust_001",
            goal="update destination",
        )
        run.blocker = "new_address_unavailable"
        run.next_loop_state = "ASK"

        resumed = store.start(
            run_id="run-1",
            scenario_id="control-ask-resume",
            customer_id="cust_001",
            goal="update destination",
        )
        self.assertIs(run, resumed)
        self.assertEqual(resumed.blocker, "new_address_unavailable")

    def test_context_snapshot_reports_actual_surface(self) -> None:
        snapshot = context_snapshot(
            variant="engineered",
            prompt="hello",
            system_prompt="system",
            message_count=2,
            tool_names=["get_order"],
        )
        self.assertEqual(snapshot["conversation_messages"], 2)
        self.assertEqual(snapshot["active_tools"], ["get_order"])
        self.assertEqual(snapshot["context_lifetime"], "this model call")
        self.assertFalse(snapshot["memory"]["selected_into_this_context"])

    def test_engineered_budget_guard_triggers_at_ninety_percent(self) -> None:
        store = RunStore()
        run = store.start(
            run_id="run-budget",
            scenario_id="control-budget-pressure",
            customer_id="cust_001",
            goal="complete a multi-step investigation",
            tool_budget=10,
        )
        record_iteration(run)
        for _index in range(9):
            record_tool_call(run, "get_order")
        self.assertEqual(run.iteration_count, 1)
        self.assertEqual(grace_threshold_calls(run), 9)
        self.assertEqual(run.dump()["tool_budget_used_percent"], 90)
        self.assertEqual(run.dump()["tool_calls_remaining"], 1)

    def test_completion_requires_all_postconditions(self) -> None:
        store = RunStore()
        run = store.start(
            run_id="run-verify",
            scenario_id="tools-net-refund",
            customer_id="cust_001",
            goal="apply shipping credit",
        )
        contract = load_skill_contract(
            ROOT / "cs_agent_engineered" / "skills", "handle-cancellation"
        )
        self.assertIsNotNone(contract)
        apply_task_contract(run, contract or {}, "skill:handle-cancellation")
        run.verified_criteria["order_observed"] = True
        complete, missing = verify_postconditions(run)
        self.assertFalse(complete)
        self.assertIn("policy_observed", missing)
        self.assertIn("refund_committed", missing)

    def test_validation_blocks_reply_with_missing_refund_prerequisites(self) -> None:
        run = RunStore().start(
            run_id="run-validation",
            scenario_id="validation-late-credit",
            customer_id="cust_001",
            goal="apply late credit",
        )
        contract = load_skill_contract(ROOT / "cs_agent_engineered" / "skills", "handle-refund")
        self.assertIsNotNone(contract)
        apply_task_contract(run, contract or {}, "skill:handle-refund")
        record_tool_call(run, "get_order", {"order_id": "1234"})
        record_observation(
            run,
            tool_name="get_order",
            observation={"status": "in_transit_delayed"},
        )
        record_tool_call(
            run, "issue_refund", {"order_id": "1234", "refund_percentage": 0.10}
        )
        record_observation(
            run,
            tool_name="issue_refund",
            observation={"ok": True, "ref": "unsafe"},
        )

        result = validate_response(run, "I applied your credit.")

        self.assertFalse(result["passed"])
        self.assertIn("policy_observed", result["missing_criteria"])
        self.assertIn(
            "refund_was_attempted_before_refund_history", result["violations"]
        )

    def test_budget_demo_fails_first_cut_and_resumes_engineered(self) -> None:
        first_store = RunStore()
        first = first_store.start(
            run_id="budget-first",
            scenario_id="control-budget-pressure",
            customer_id="cust_001",
            goal="investigate safely",
            tool_budget=10,
        )
        first_events = list(
            stream_budget_investigation(
                run=first,
                variant="first_cut",
                agent_id="test_budget_first",
                customer_id="cust_001",
                framed_prompt="investigate",
            )
        )
        self.assertEqual(first.status, "error")
        self.assertEqual(first.tool_call_count, 10)
        self.assertEqual(first.iteration_count, 1)
        self.assertTrue(any(event["event"] == "error" for event in first_events))

        engineered_store = RunStore()
        engineered = engineered_store.start(
            run_id="budget-engineered",
            scenario_id="control-budget-pressure",
            customer_id="cust_001",
            goal="investigate safely",
            tool_budget=10,
        )
        list(
            stream_budget_investigation(
                run=engineered,
                variant="engineered",
                agent_id="test_budget_engineered",
                customer_id="cust_001",
                framed_prompt="investigate",
            )
        )
        self.assertEqual(engineered.status, "paused")
        self.assertEqual(engineered.tool_call_count, 9)

        resumed = engineered_store.start(
            run_id="budget-engineered",
            scenario_id="control-budget-pressure",
            customer_id="cust_001",
            goal="investigate safely",
            tool_budget=10,
        )
        list(
            stream_budget_investigation(
                run=resumed,
                variant="engineered",
                agent_id="test_budget_engineered",
                customer_id="cust_001",
                framed_prompt="continue",
            )
        )
        self.assertEqual(resumed.status, "complete")
        self.assertEqual(resumed.tool_call_count, 2)
        self.assertIsNone(resumed.resume_condition)


class RecoveryTests(unittest.TestCase):
    def test_operation_id_makes_refund_idempotent(self) -> None:
        agent_id = "test_loop_idempotency"
        reset_data_files(agent_id)
        client = CustomerSupportClient(agent_id=agent_id)
        first = client.issue_refund(
            "1234",
            "cust_001",
            8.95,
            "test",
            agent_id,
            refund_percentage=0.10,
            operation_id="op-1",
        )
        second = client.issue_refund(
            "1234",
            "cust_001",
            8.95,
            "retry",
            agent_id,
            refund_percentage=0.10,
            operation_id="op-1",
        )
        writes = [
            entry
            for entry in client.ledger_entries()
            if entry.get("actor_agent_id") == agent_id and entry.get("kind") == "refund"
        ]
        self.assertEqual(first, second)
        self.assertEqual(len(writes), 1)

    def test_evaluation_suite_is_computed(self) -> None:
        suite = run_suite(ROOT)
        self.assertEqual(suite["summary"]["total"], len(suite["results"]))
        self.assertEqual(suite["summary"]["total"], 13)
        self.assertEqual(suite["summary"]["engineered"], 13)
        self.assertGreater(suite["summary"]["engineered"], suite["summary"]["first_cut"])

    def test_evaluation_covers_every_runnable_ui_scenario(self) -> None:
        suite = run_suite(ROOT)
        evaluated = {item["scenario_id"] for item in suite["results"]}
        expected = {
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
        }
        self.assertEqual(evaluated, expected)

    def test_expected_first_cut_and_engineered_outcomes_are_explicit(self) -> None:
        suite = run_suite(ROOT)
        self.assertTrue(
            all(
                item["expected_behavior"]["first_cut"]
                and item["expected_behavior"]["engineered"]
                for item in suite["results"]
            )
        )
        outcomes = {
            item["scenario_id"]: (
                item["first_cut"]["passed"],
                item["engineered"]["passed"],
            )
            for item in suite["results"]
        }
        self.assertEqual(
            outcomes,
            {
                "context-repeat-damage": (False, True),
                "tools-refund-history": (False, True),
                "tools-net-refund": (False, True),
                "skills-address-change": (False, True),
                "memory-promise-lapse": (False, True),
                "memory-scope-action": (False, True),
                "control-conditional-plan": (False, True),
                "safety-identity-binding": (False, True),
                "control-ask-resume": (False, True),
                "control-budget-pressure": (False, True),
                "recovery-timeout-after-commit": (False, True),
                "validation-damaged-item": (False, True),
                "validation-late-credit": (False, True),
            },
        )


if __name__ == "__main__":
    unittest.main()
