"""Checks that the guardrail integration tests cannot pass or fail unguarded.

backend/tests/integration/test_guardrails_integration.py asserts on orchestrator
replies. The model applies a guardrail only when one is enabled and configured,
so without one those replies come from an unguarded model and the assertions
test model wording instead of the guardrail.

- The module skips unless a guardrail is configured (enabled, ID, version).
- Failure messages stringify the AgentResult before slicing it, so a failing
  assertion reports the reply instead of raising TypeError.

These are static checks; nothing here imports the backend or calls AWS.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPOSITORY_ROOT / "backend" / "tests" / "integration" / "test_guardrails_integration.py"


class GuardrailIntegrationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MODULE.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def _module_assignment(self, name: str) -> ast.expr:
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            ):
                return node.value
        self.fail(f"{MODULE.name} must assign {name} at module level")

    def test_guardrail_configuration_matches_the_model_condition(self) -> None:
        condition = ast.unparse(self._module_assignment("GUARDRAIL_CONFIGURED"))
        for setting in ("BEDROCK_GUARDRAIL_ENABLED", "BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION"):
            self.assertIn(setting, condition)

    def test_module_skips_without_a_configured_guardrail(self) -> None:
        pytestmark = self._module_assignment("pytestmark")
        skipifs = [
            node
            for node in ast.walk(pytestmark)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "skipif"
        ]
        self.assertEqual(len(skipifs), 1, "pytestmark needs one skipif for an unconfigured guardrail")
        self.assertEqual(ast.unparse(skipifs[0].args[0]), "not GUARDRAIL_CONFIGURED")
        reason = next((kw.value for kw in skipifs[0].keywords if kw.arg == "reason"), None)
        self.assertIsNotNone(reason, "the skip must explain how to configure the guardrail")
        self.assertIn("GBAW_BEDROCK_GUARDRAIL_ID", ast.unparse(reason))

    def test_failure_messages_do_not_slice_the_agent_result(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "response":
                self.fail(f"line {node.lineno}: slice str(response), not the AgentResult itself")


if __name__ == "__main__":
    unittest.main()
