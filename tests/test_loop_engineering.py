import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from context_compaction import CompactAt, MessageWindow, projected_input_tokens
from loop_state import (
    RunStore,
    context_snapshot,
    grant_budget,
    token_ceiling,
    token_warning_threshold,
    record_compaction,
    record_iteration,
    record_tool_call,
    record_human_decision,
    record_observation,
)
from run_control import TokenBudgetHook
from mocks.client import CustomerSupportClient, arm_fault, reset_data_files
from policy_evaluator import (
    ASPECTS as EVALUATION_ASPECTS,
    _normalize as normalize_aspect_result,
    aggregate_verdict,
    evaluate_turn,
)
from cs_agent_engineered.agent.hooks import (
    CONFIRM_BEFORE_TOOLS,
    HumanConfirmationHook,
    confirmation_card,
    confirmation_question,
    is_affirmative,
)


ROOT = Path(__file__).resolve().parents[1]


class LoopStateTests(unittest.TestCase):
    def test_aspect_result_normalizes_an_explainable_pass(self) -> None:
        result = normalize_aspect_result(
            "policy",
            {
                "verdict": "pass",
                "summary": "The observed refund matches the applicable policy.",
                "findings": [],
            },
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["aspect"], "policy")
        self.assertEqual(result["label"], EVALUATION_ASPECTS["policy"][0])

    def test_aspect_result_non_pass_is_never_reasonless(self) -> None:
        for verdict in ("warn", "fail"):
            with self.subTest(verdict=verdict):
                result = normalize_aspect_result(
                    "groundedness",
                    {"verdict": verdict, "summary": "Claimed a refund that never ran.", "findings": []},
                )
                self.assertFalse(result["passed"])
                self.assertTrue(result["findings"])

    def test_aspect_result_rejects_an_unknown_verdict(self) -> None:
        with self.assertRaises(ValueError):
            normalize_aspect_result("policy", {"verdict": "maybe", "summary": "unsure"})

    def test_aggregate_reports_the_worst_aspect(self) -> None:
        self.assertEqual(aggregate_verdict([{"verdict": "pass"}, {"verdict": "pass"}]), "pass")
        self.assertEqual(aggregate_verdict([{"verdict": "pass"}, {"verdict": "warn"}]), "warn")
        self.assertEqual(
            aggregate_verdict([{"verdict": "fail"}, {"verdict": "warn"}, {"verdict": "pass"}]),
            "fail",
        )
        # An unreachable reviewer outranks a pass: the turn is not cleared.
        self.assertEqual(aggregate_verdict([{"verdict": "pass"}, {"verdict": "unavailable"}]), "unavailable")
        self.assertEqual(aggregate_verdict([]), "unavailable")

    def test_evaluate_turn_runs_every_aspect_and_sums_tokens(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"verdict":"pass","summary":"Observed actions match.","findings":[]}'
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=400, completion_tokens=100),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=response))
            )
        )

        async def collect():
            out = []
            with patch("policy_evaluator.AsyncOpenAI", return_value=client):
                async for result, tokens in evaluate_turn(
                    model="test-model",
                    customer_request="Where is my order?",
                    proposed_reply="It is in transit.",
                    tool_history=[{"name": "get_order", "is_error": False}],
                    declared_contract={"required_criteria": ["order_observed"]},
                    refund_cap_usd=200.0,
                ):
                    out.append((result, tokens))
            return out

        results = asyncio.run(collect())
        self.assertEqual(len(results), len(EVALUATION_ASPECTS))
        self.assertEqual({r["aspect"] for r, _ in results}, set(EVALUATION_ASPECTS))
        self.assertEqual(
            sum(usage.output_tokens for _, usage in results), 100 * len(EVALUATION_ASPECTS)
        )
        self.assertEqual(
            sum(usage.input_tokens for _, usage in results), 400 * len(EVALUATION_ASPECTS)
        )

    def test_evaluate_turn_isolates_a_failing_reviewer(self) -> None:
        """One unreachable review must not take the other aspects down."""
        calls = {"n": 0}

        async def flaky(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("reviewer unreachable")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"verdict":"pass","summary":"Fine.","findings":[]}'
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=40, completion_tokens=10),
            )

        create = AsyncMock(side_effect=flaky)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        async def collect():
            out = []
            with patch("policy_evaluator.AsyncOpenAI", return_value=client):
                async for result, _usage in evaluate_turn(
                    model="test-model",
                    customer_request="q",
                    proposed_reply="a",
                    tool_history=[],
                    declared_contract=None,
                    refund_cap_usd=200.0,
                ):
                    out.append(result)
            return out

        results = asyncio.run(collect())
        self.assertEqual(len(results), len(EVALUATION_ASPECTS))
        verdicts = sorted(r["verdict"] for r in results)
        self.assertIn("unavailable", verdicts)
        self.assertEqual(verdicts.count("pass"), len(EVALUATION_ASPECTS) - 1)
        # An unavailable aspect is neither a pass nor a failure.
        unavailable = next(r for r in results if r["verdict"] == "unavailable")
        self.assertIsNone(unavailable["passed"])
        request = client.chat.completions.create.await_args.kwargs
        self.assertEqual(request["response_format"], {"type": "json_object"})

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
            token_budget=3000,
        )
        record_iteration(run)
        for _index in range(4):
            record_tool_call(run, "get_order")
        run.output_tokens = 700
        run.input_tokens = 2_000
        self.assertEqual(run.iteration_count, 1)
        self.assertEqual(token_warning_threshold(run), 2700)
        self.assertEqual(run.dump()["token_budget_used_percent"], 90)
        # The last tenth is what pays for the wrap-up call.
        self.assertEqual(run.dump()["tokens_remaining"], 300)

    def test_a_parked_write_is_one_row_not_two(self) -> None:
        """The re-dispatch after human approval is the same call, not a retry.

        `BeforeToolCallEvent` fires once before the confirmation interrupt and
        again on resume. Counted twice, the trajectory grows a resultless ghost
        row that reads to a reviewer as a failed attempt.
        """
        store = RunStore()
        run = store.start(
            run_id="run-confirm",
            customer_id="cust_001",
            goal="change my delivery address",
            token_budget=20_000,
        )
        # First pass: the model's raw args, before the binding hook fills in
        # the trusted customer_id, and before the interrupt stops the call.
        record_tool_call(
            run,
            "update_shipping_address",
            {"customer_id": "", "order_id": "1240"},
            tool_use_id="tu_1",
        )
        record_human_decision(
            run, tool_name="update_shipping_address", approved=True, tool_use_id="tu_1"
        )
        # Resume: same toolUseId, bound args, and this time it runs.
        record_tool_call(
            run,
            "update_shipping_address",
            {"customer_id": "cust_001", "order_id": "1240"},
            tool_use_id="tu_1",
        )
        record_observation(
            run,
            tool_name="update_shipping_address",
            observation={"ok": True, "ref": "addr_0004"},
            tool_use_id="tu_1",
        )

        calls = [item for item in run.tool_history if item["name"] == "update_shipping_address"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.tool_call_count, 1)
        self.assertEqual(calls[0]["args"]["customer_id"], "cust_001")
        self.assertEqual(calls[0]["result"], {"ok": True, "ref": "addr_0004"})
        approval = [item for item in run.tool_history if item["name"] == "human_approval"]
        self.assertEqual(approval[0]["result"], {"approved": True})
        self.assertEqual(approval[0]["args"]["gated_tool"], "update_shipping_address")

    def test_parallel_writes_keep_their_own_results(self) -> None:
        """Two writes in one turn differ only by argument, so pair them by id."""
        store = RunStore()
        run = store.start(
            run_id="run-parallel",
            customer_id="cust_001",
            goal="change my delivery address",
            token_budget=20_000,
        )
        record_tool_call(run, "update_shipping_address", {"order_id": "1240"}, tool_use_id="tu_a")
        record_tool_call(run, "update_shipping_address", {"order_id": "1241"}, tool_use_id="tu_b")
        # Out of order on purpose: results do not have to come back in the
        # order the calls were issued.
        record_observation(
            run,
            tool_name="update_shipping_address",
            observation={"ref": "addr_0005"},
            tool_use_id="tu_b",
        )
        record_observation(
            run,
            tool_name="update_shipping_address",
            observation={"ref": "addr_0004"},
            tool_use_id="tu_a",
        )

        by_order = {item["args"]["order_id"]: item["result"]["ref"] for item in run.tool_history}
        self.assertEqual(by_order, {"1240": "addr_0004", "1241": "addr_0005"})
        self.assertEqual(run.tool_call_count, 2)

    def test_budget_grant_resumes_the_same_task_with_a_bigger_ceiling(self) -> None:
        """A "yes" must not clear the meter, or the task could never finish."""
        store = RunStore()
        run = store.start(
            run_id="run-grant",
            customer_id="cust_001",
            goal="investigate a late order",
            token_budget=20_000,
        )
        run.output_tokens = 18_500
        run.pending_request = "investigate a late order"
        record_tool_call(run, "get_order")

        ceiling = grant_budget(run)
        resumed = store.start(
            run_id="run-grant",
            customer_id="cust_001",
            goal="Yes, continue.",
            token_budget=20_000,
            resume=True,
        )

        self.assertIs(run, resumed)
        self.assertEqual(ceiling, 40_000)
        self.assertEqual(token_ceiling(resumed), 40_000)
        # Spend and evidence carry over; only the turn-local counters reset.
        self.assertEqual(resumed.output_tokens, 18_500)
        self.assertEqual(len(resumed.tool_history), 1)
        self.assertEqual(resumed.model_call_count, 0)
        self.assertEqual(resumed.goal, "investigate a late order")
        self.assertEqual(token_warning_threshold(resumed), 36_000)

        # A later message on the same run is another turn in the same chat:
        # session usage remains cumulative.
        next_turn = store.start(
            run_id="run-grant",
            customer_id="cust_001",
            goal="something else",
            token_budget=20_000,
        )
        self.assertEqual(next_turn.output_tokens, 18_500)
        self.assertEqual(next_turn.budget_grants, 1)
        self.assertEqual(token_ceiling(next_turn), 40_000)

    def test_a_confirmation_resume_continues_the_turn_it_paused(self) -> None:
        """A parked write resumes the same turn, so its meter is not restarted.

        The console streams what follows a confirmation into the card the
        customer is already looking at. If the per-turn counters reset with it,
        that card ends up reporting only the model calls made after the
        approval and silently drops the ones that led up to it.
        """
        store = RunStore()
        run = store.start(
            run_id="run-confirm",
            customer_id="cust_001",
            goal="change my delivery address",
            token_budget=20_000,
        )
        run.model_call_usage = [
            {"call": 1, "input_tokens": 900, "output_tokens": 40, "total_tokens": 940,
             "fixed_context_tokens": 800, "dynamic_context_tokens": 100},
            {"call": 2, "input_tokens": 1_400, "output_tokens": 60, "total_tokens": 1_460,
             "fixed_context_tokens": 800, "dynamic_context_tokens": 600},
        ]
        run.model_call_count = 2
        record_tool_call(run, "get_customer_orders")

        resumed = store.start(
            run_id="run-confirm",
            customer_id="cust_001",
            goal="",
            token_budget=20_000,
            continue_turn=True,
        )
        self.assertIs(run, resumed)
        self.assertEqual(len(resumed.model_call_usage), 2)
        self.assertEqual(resumed.model_call_count, 2)
        self.assertEqual(resumed.tool_call_count, 1)
        # The pause is released either way: the loop is running again.
        self.assertEqual(resumed.status, "running")
        self.assertIsNone(resumed.exit_reason)

        # A genuinely new turn on the same run still starts its own counters.
        fresh = store.start(
            run_id="run-confirm",
            customer_id="cust_001",
            goal="something else",
            token_budget=20_000,
        )
        self.assertEqual(fresh.model_call_usage, [])
        self.assertEqual(fresh.tool_call_count, 0)

class HumanConfirmationTests(unittest.TestCase):
    """The harness, not the model, decides whether the customer said yes."""

    def test_affirmative_answers_approve(self) -> None:
        for reply in ("yes", "Yes please", "confirm", "go ahead", "ok do it"):
            self.assertTrue(is_affirmative(reply), reply)

    def test_anything_else_denies(self) -> None:
        for reply in ("no", "not yet", "don't cancel it", "hold on", "", "what is my ETA?"):
            self.assertFalse(is_affirmative(reply), reply)

    def test_a_negative_beats_a_yes_in_the_same_reply(self) -> None:
        self.assertFalse(is_affirmative("yes I want a refund, but no, don't cancel the order"))

    def test_both_write_tools_are_gated(self) -> None:
        self.assertEqual(
            CONFIRM_BEFORE_TOOLS,
            frozenset({"update_shipping_address", "cancel_order"}),
        )

    def test_hook_asks_once_then_honours_the_answer(self) -> None:
        """First pass interrupts; the resumed pass runs or cancels the tool."""

        class FakeEvent:
            def __init__(self, answer):
                self.tool_use = {
                    "name": "cancel_order",
                    "input": {"customer_id": "cust_001", "order_id": "1243", "reason": "late"},
                }
                self.cancel_tool = False
                self.asked = None
                self._answer = answer

            def interrupt(self, name, reason=None, response=None):
                self.asked = reason
                if self._answer is None:
                    raise RuntimeError("would have paused the loop")
                return self._answer

        hook = HumanConfirmationHook()

        paused = FakeEvent(None)
        with self.assertRaises(RuntimeError):
            hook._require_confirmation(paused)
        self.assertEqual(paused.asked["tool"], "cancel_order")
        self.assertNotIn("customer_id", paused.asked["args"])

        approved = FakeEvent({"approved": True, "customer_reply": "yes"})
        hook._require_confirmation(approved)
        self.assertFalse(approved.cancel_tool)

        denied = FakeEvent({"approved": False, "customer_reply": "no, just fix the address"})
        hook._require_confirmation(denied)
        self.assertIn("confirmation_denied", denied.cancel_tool)

    def test_a_call_already_rejected_is_not_put_to_the_customer(self) -> None:
        class RejectedEvent:
            tool_use = {"name": "cancel_order", "input": {"order_id": "1243"}}
            cancel_tool = "{\"error\": \"unauthorized\"}"

            def interrupt(self, *_a, **_kw):
                raise AssertionError("must not ask about a call that cannot run")

        HumanConfirmationHook()._require_confirmation(RejectedEvent())

    def test_question_names_the_actual_action(self) -> None:
        question = confirmation_question("cancel_order", {"order_id": "1243"})
        self.assertIn("cancel order #1243", question)
        address = confirmation_question(
            "update_shipping_address", {"order_id": "1243", "new_address": "12 Bell St"}
        )
        self.assertIn("12 Bell St", address)
        self.assertIn("#1243", address)

    def test_card_describes_the_call_the_tool_will_receive(self) -> None:
        """The consent control is built from the args, not from prose.

        The customer approves what the card shows, so every value in it has to
        come from the same input dict the tool is about to be handed.
        """
        card = confirmation_card(
            "update_shipping_address",
            {"customer_id": "cust_001", "order_id": "1243", "new_address": "12 Bell St"},
        )
        values = [f["value"] for f in card["fields"]]
        self.assertIn("12 Bell St", values)
        self.assertIn("#1243", values)
        self.assertTrue(card["title"])
        self.assertTrue(card["effect"])
        # customer_id is harness-owned; showing it would invite the customer to
        # check an identifier they never chose.
        self.assertNotIn("cust_001", values)

    def test_a_write_with_no_wording_is_still_shown_in_full(self) -> None:
        """A newly gated tool must stay legible before it is approved."""
        card = confirmation_card("close_account", {"customer_id": "cust_001", "force": True})
        self.assertEqual([f["value"] for f in card["fields"]], ["True"])
        self.assertIn("close account", card["title"].lower())

    def test_parallel_writes_are_two_separate_decisions(self) -> None:
        """Two writes in one model step park as two interrupts, not one.

        Strands scopes the interrupt id to the toolUseId, so approving one
        address change cannot carry the other one through with it.
        """

        class FakeEvent:
            def __init__(self, tool_use_id, order_id, answer):
                self.tool_use = {
                    "toolUseId": tool_use_id,
                    "name": "update_shipping_address",
                    "input": {
                        "customer_id": "cust_001",
                        "order_id": order_id,
                        "new_address": "18 Harbour Street",
                    },
                }
                self.cancel_tool = False
                self.asked = None
                self._answer = answer

            def interrupt(self, name, reason=None, response=None):
                self.asked = reason
                return self._answer

        hook = HumanConfirmationHook()
        approved = FakeEvent("tu_a", "1241", {"approved": True})
        refused = FakeEvent("tu_b", "1240", {"approved": False, "customer_reply": "only 1241"})
        hook._require_confirmation(approved)
        hook._require_confirmation(refused)

        self.assertFalse(approved.cancel_tool)
        self.assertIn("confirmation_denied", refused.cancel_tool)
        # Each question names its own order, so the two cards cannot be read
        # as one decision about "the address change".
        self.assertIn("#1241", approved.asked["question"])
        self.assertIn("#1240", refused.asked["question"])

    def test_each_decision_names_the_call_it_gates(self) -> None:
        """The console pins the consent control to a trace row, so the parked
        call's `toolUseId` has to travel with the question."""

        class FakeEvent:
            tool_use = {
                "toolUseId": "tu_a",
                "name": "cancel_order",
                "input": {"customer_id": "cust_001", "order_id": "1243"},
            }
            cancel_tool = False
            asked = None

            def interrupt(self, name, reason=None, response=None):
                type(self).asked = reason
                return {"approved": True}

        HumanConfirmationHook()._require_confirmation(FakeEvent())
        self.assertEqual(FakeEvent.asked["tool_use_id"], "tu_a")


class DataFileConcurrencyTests(unittest.TestCase):
    """The MCP servers are separate processes reading these same files."""

    def test_reset_never_exposes_a_missing_or_partial_file(self) -> None:
        """A tool call landing during a reset must still see a whole file.

        The old reset unlinked each file before rewriting it, which is what
        surfaced as `[Errno 2] No such file or directory: orders.json` on a
        tool call that happened to run at that moment.
        """
        import threading

        agent_id = "test_reset_race"
        reset_data_files(agent_id)
        from mocks import client as mock_client
        orders = mock_client.DATA_ROOT / agent_id / "orders.json"

        failures: list[BaseException] = []
        stop = threading.Event()

        def read_forever() -> None:
            while not stop.is_set():
                try:
                    json.loads(orders.read_text())
                except BaseException as exc:  # noqa: BLE001
                    failures.append(exc)
                    return

        reader = threading.Thread(target=read_forever)
        reader.start()
        try:
            for _ in range(60):
                reset_data_files(agent_id)
        finally:
            stop.set()
            reader.join(timeout=5)

        self.assertEqual(failures, [], f"reader saw a broken file: {failures[:1]}")


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

class RefundEntitlementTests(unittest.TestCase):
    """The reason-code gate: every refund names an entitlement and every
    entitlement is checked against stored order facts.

    These exercise the MCP server's own check. `RefundEvidenceHook` mirrors it
    in the harness so the call never reaches the wire, but the server is the
    control that holds even if the hook is not registered.
    """

    @staticmethod
    def _server():
        import importlib.util
        import sys

        # The server resolves `agent.identity` relative to its own service
        # root, the way its MCP subprocess does.
        service_root = str(ROOT / "cs_agent_engineered")
        if service_root not in sys.path:
            sys.path.insert(0, service_root)
        path = ROOT / "cs_agent_engineered/mcp_servers/customer_support_engineered/__main__.py"
        spec = importlib.util.spec_from_file_location("cs_support_server_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Swap the module-level client onto an isolated data dir. Left as
        # imported it would issue refunds and cancel orders against the live
        # demo state, so `make test` would quietly dirty the demo.
        agent_id = "test_refund_entitlement"
        reset_data_files(agent_id)
        module._client = CustomerSupportClient(agent_id=agent_id)
        module._identity.agent_id = agent_id
        return module

    def test_damaged_refund_blocked_without_photo_evidence(self) -> None:
        server = self._server()
        result = server.issue_refund("cust_001", "1243", 1.0, "damaged", "test")
        self.assertEqual(result.get("error"), "policy_violation")
        self.assertEqual(result.get("code"), 422)
        self.assertIn("no damage photos on file", result.get("detail", ""))

    def test_damaged_refund_allowed_when_photos_on_file(self) -> None:
        server = self._server()
        result = server.issue_refund("cust_001", "1244", 1.0, "damaged", "test")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("reason_code"), "damaged")

    def test_refund_timeout_is_structured_and_does_not_commit(self) -> None:
        server = self._server()
        arm_fault(server._identity.agent_id, "refund_service_timeout")
        result = server.issue_refund("cust_001", "1244", 1.0, "damaged", "test")
        writes = [
            entry
            for entry in server._client.ledger_entries()
            if entry.get("actor_agent_id") == server._identity.agent_id
            and entry.get("kind") == "refund"
        ]
        self.assertEqual(result.get("error"), "service_timeout")
        self.assertEqual(result.get("outcome"), "not_committed")
        self.assertFalse(result.get("retryable"))
        self.assertEqual(result.get("remediation"), "escalate_to_human")
        self.assertEqual(len(writes), 0)

    def test_no_reason_code_launders_a_blocked_damage_claim(self) -> None:
        """The property the whole design rests on: an agent blocked on
        `damaged` cannot re-claim the same refund under another code."""
        server = self._server()
        for code in ("cancellation", "shipping_delay_credit", "return"):
            with self.subTest(code=code):
                result = server.issue_refund("cust_001", "1243", 1.0, code, "test")
                self.assertFalse(result.get("ok"), f"{code} laundered a blocked damage claim")
        invented = server.issue_refund("cust_001", "1243", 1.0, "goodwill", "test")
        self.assertEqual(invented.get("error"), "invalid_reason_code")

    def test_cap_takes_precedence_over_entitlement(self) -> None:
        """#1239 fails both checks; the cap is the harder limit and should be
        the one reported, so over-cap refunds read the same whatever code."""
        server = self._server()
        result = server.issue_refund("cust_001", "1239", 1.0, "damaged", "test")
        self.assertEqual(result.get("code"), 403)

    def test_cancellation_refund_requires_the_cancellation_to_have_happened(self) -> None:
        server = self._server()
        before = server.issue_refund("cust_001", "1241", 0.90, "cancellation", "test")
        self.assertEqual(before.get("code"), 422)
        server._client.cancel_order("1241", "test", server._identity.agent_id)
        server._client._reload_cache()
        after = server.issue_refund("cust_001", "1241", 0.90, "cancellation", "test")
        self.assertTrue(after.get("ok"), after)

    def test_shipping_delay_credit_honours_the_three_day_threshold(self) -> None:
        server = self._server()
        self.assertTrue(
            server.issue_refund("cust_001", "1234", 0.10, "shipping_delay_credit", "test").get("ok")
        )
        self.assertEqual(
            server.issue_refund("cust_001", "1238", 0.10, "shipping_delay_credit", "test").get("code"),
            422,
        )

if __name__ == "__main__":
    unittest.main()


class AutoCompactionTests(unittest.TestCase):
    """The compaction dial: when it fires, what it spares, what it reports."""

    def _agent(self, messages: list, run) -> SimpleNamespace:
        """An agent stand-in with the three surfaces the pipeline reads."""

        async def count_tokens(msgs, tool_specs=None, system_prompt=None, **_):
            # 10 tokens per message, plus a fixed system + tools surface, so a
            # threshold can sit above the irreducible baseline the way a real
            # one has to.
            return 1_000 + 10 * len(msgs)

        return SimpleNamespace(
            messages=messages,
            model=SimpleNamespace(count_tokens=count_tokens, context_window_limit=400_000),
            tool_registry=SimpleNamespace(get_all_tool_specs=lambda: []),
            system_prompt="system",
            _active_run_state=run,
        )

    def _context(self, agent, overflow: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            messages=agent.messages, agent=agent, utilization=0.0, overflow=overflow, stash=None
        )

    def _run(self, compact_at: int | None):
        store = RunStore()
        return store.start(
            run_id="run-compact",
            customer_id="cust_001",
            goal="a long task",
            compact_at=compact_at,
        )

    def test_dial_off_never_summarizes(self) -> None:
        run = self._run(None)
        agent = self._agent([{"role": "user", "content": []} for _ in range(40)], run)
        inner = AsyncMock(return_value=True)
        strategy = CompactAt(SimpleNamespace(apply=inner))
        acted = asyncio.run(strategy.apply(self._context(agent)))
        self.assertFalse(acted)
        inner.assert_not_awaited()
        self.assertEqual(run.compactions, [])

    def test_below_the_line_never_summarizes(self) -> None:
        run = self._run(10_000)
        agent = self._agent([{"role": "user", "content": []} for _ in range(20)], run)
        inner = AsyncMock(return_value=True)
        acted = asyncio.run(CompactAt(SimpleNamespace(apply=inner)).apply(self._context(agent)))
        self.assertFalse(acted)
        inner.assert_not_awaited()

    def test_crossing_the_line_summarizes_and_reports_the_saving(self) -> None:
        run = self._run(2_000)
        messages = [{"role": "user", "content": []} for _ in range(120)]
        agent = self._agent(messages, run)

        async def summarize(context):
            del context.messages[: len(context.messages) - 5]
            return True

        acted = asyncio.run(CompactAt(SimpleNamespace(apply=summarize)).apply(self._context(agent)))
        self.assertTrue(acted)
        self.assertEqual(len(run.compactions), 1)
        entry = run.compactions[0]
        self.assertEqual(entry["before_tokens"], 2_200)
        self.assertEqual(entry["after_tokens"], 1_050)
        self.assertEqual(entry["saved_tokens"], 1_150)
        self.assertEqual(entry["messages_summarized"], 115)
        # Keyed to the call it protects, which is the one the budget guard is
        # about to count.
        self.assertEqual(entry["call"], run.model_call_count + 1)

    def test_a_context_still_above_the_line_is_not_compacted_twice(self) -> None:
        """Otherwise every model call buys another summarization call to
        re-summarize the summary the last one just wrote."""
        # A line the compacted context is still above, so only the guard can
        # stop the second pass.
        run = self._run(1_000)
        messages = [{"role": "user", "content": []} for _ in range(120)]
        agent = self._agent(messages, run)

        async def summarize(context):
            del context.messages[: len(context.messages) - 5]
            return True

        strategy = CompactAt(SimpleNamespace(apply=summarize))
        self.assertTrue(asyncio.run(strategy.apply(self._context(agent))))
        # Still above the line, and a tool loop has added a call and its result
        # — but folding those two in would save almost nothing.
        agent.messages.extend({"role": "user", "content": []} for _ in range(2))
        self.assertFalse(asyncio.run(strategy.apply(self._context(agent))))
        # Once the context has grown materially past what the last compaction
        # left behind, another pass is worth its model call.
        agent.messages.extend({"role": "user", "content": []} for _ in range(120))
        self.assertTrue(asyncio.run(strategy.apply(self._context(agent))))
        self.assertEqual(len(run.compactions), 2)

    def test_the_line_is_measured_against_the_providers_own_baseline(self) -> None:
        """Strands' estimator roughly doubles this agent's tool contracts, so a
        dial fed by the raw estimate would fire at half the context the console
        is drawing. Once a call has completed, the exact fixed surface is known."""
        run = self._run(10_000)
        agent = self._agent([{"role": "user", "content": []} for _ in range(20)], run)
        # 1,000 estimated fixed + 200 of messages: over the line on the estimate
        # alone, but the provider says the fixed surface is really 300.
        self.assertEqual(asyncio.run(projected_input_tokens(agent)), 1_200)
        agent._fixed_context_baseline_tokens = 300
        self.assertEqual(asyncio.run(projected_input_tokens(agent)), 1_500)

    def test_a_real_overflow_compacts_even_with_the_dial_off(self) -> None:
        run = self._run(None)
        messages = [{"role": "user", "content": []} for _ in range(30)]
        agent = self._agent(messages, run)

        async def summarize(context):
            del context.messages[:20]
            return True

        acted = asyncio.run(
            CompactAt(SimpleNamespace(apply=summarize)).apply(self._context(agent, overflow=True))
        )
        self.assertTrue(acted)
        self.assertTrue(run.compactions[0]["overflow"])

    def test_the_budget_guard_meters_the_compacted_context(self) -> None:
        """Strands projects the next call before hooks run, so the number it
        hands the guard predates a compaction at the same event."""
        run = self._run(2_000)
        run.token_budget = 40_000
        record_compaction(
            run,
            before_tokens=12_000,
            after_tokens=3_000,
            messages_before=40,
            messages_after=6,
            threshold=2_000,
        )
        agent = SimpleNamespace(_active_run_state=run)
        event = SimpleNamespace(agent=agent, projected_input_tokens=12_000, cancel=None)
        TokenBudgetHook(mode="graceful")._before_model(event)
        self.assertEqual(run.projected_next_call_tokens, 3_000)
        self.assertIsNone(event.cancel)

    def test_the_session_window_stands_down_while_compaction_is_armed(self) -> None:
        """Dropping the oldest messages and then summarizing what is left would
        mean summarizing evidence that had already been deleted."""
        run = self._run(10_000)
        agent = self._agent([{"role": "user", "content": []} for _ in range(50)], run)
        window = MessageWindow(window_size=4)
        self.assertFalse(asyncio.run(window.apply(self._context(agent))))
        self.assertEqual(len(agent.messages), 50)

    def test_the_session_window_still_caps_when_the_dial_is_off(self) -> None:
        run = self._run(None)
        messages = [
            {"role": "user", "content": [{"text": "hi"}]},
            {"role": "assistant", "content": [{"text": "hello"}]},
        ] * 15
        agent = self._agent(messages, run)
        window = MessageWindow(window_size=4)
        self.assertTrue(asyncio.run(window.apply(self._context(agent))))
        self.assertLessEqual(len(agent.messages), 5)
