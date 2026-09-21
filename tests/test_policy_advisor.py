import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_policy_advisor():
    path = ROOT / "cs_agent_engineered/mcp_servers/policy_kb/__main__.py"
    spec = importlib.util.spec_from_file_location("policy_advisor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyAdvisorTests(unittest.TestCase):
    def test_requires_a_concrete_question_without_calling_a_model(self) -> None:
        module = load_policy_advisor()
        with patch.object(module, "OpenAI") as openai:
            result = module.check_policy("Damaged item", [])
        self.assertEqual(result["error"], "invalid_policy_question")
        openai.assert_not_called()

    def test_returns_a_compact_cited_decision_brief(self) -> None:
        module = load_policy_advisor()
        payload = {
            "decision": "allowed_after_conditions",
            "applicable_policies": [
                {
                    "id": "refund_damaged_item",
                    "title": "Damaged items",
                    "applies_because": "The selected order is recorded as damaged.",
                },
                {
                    "id": "invented_policy",
                    "title": "Invented",
                    "applies_because": "Should be filtered out.",
                },
            ],
            "conditions": ["Accepted photo evidence must be on the selected order."],
            "required_steps": [
                "Ask the customer to upload a photo.",
                "Escalate the missing-photo exception at normal priority.",
            ],
            "prohibited_actions": ["Do not issue a refund before evidence is on file."],
            "facts_to_verify": [],
            "customer_explanation": "A photo is required before an automatic refund.",
        }
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload))
                )
            ]
        )
        client = MagicMock()
        client.chat.completions.create.return_value = response

        with patch.object(module, "OpenAI", return_value=client):
            result = module.check_policy(
                "The French press arrived smashed and I want a refund.",
                [
                    "Order 1243 is delivered_damaged and has no photo evidence on file. "
                    "What must happen before a refund?"
                ],
            )

        self.assertEqual(result["decision"], "allowed_after_conditions")
        self.assertEqual(
            [item["id"] for item in result["applicable_policies"]],
            ["refund_damaged_item"],
        )
        self.assertNotIn("rule", result)
        self.assertNotIn("usage", result)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"], {"type": "json_object"})
        model_input = request["messages"][1]["content"]
        self.assertIn("<candidate_policies>", model_input)
        self.assertIn('id="refund_damaged_item"', model_input)


if __name__ == "__main__":
    unittest.main()
