"""Checks that the guardrail integration tests cannot pass or fail unguarded.

backend/tests/integration/test_guardrails_integration.py asserts on orchestrator
replies. The model applies a guardrail only when one is enabled and configured,
so without one those replies come from an unguarded model and the assertions
test model wording instead of the guardrail.

- The module skips unless the model would attach a guardrail, using the same
  predicate the model uses (``models.cached_bedrock.guardrail_attached``).
- No assertion prints a reply: the reply text is never part of an ``assert``
  statement (its test or its message), so neither the message nor pytest's
  assertion rewriting can put live provider data in test output.

These are static checks; nothing here imports the backend or calls AWS.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPOSITORY_ROOT / "backend" / "tests" / "integration" / "test_guardrails_integration.py"
MODEL = REPOSITORY_ROOT / "backend" / "src" / "models" / "cached_bedrock.py"

# Names the module uses for a reply or a value derived from its text.
REPLY_NAMES = {"response", "response_lower", "response_str", "text", "reply", "lowered"}


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined at module level")


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

    def test_guardrail_configuration_is_the_model_predicate(self) -> None:
        self.assertEqual(ast.unparse(self._module_assignment("GUARDRAIL_CONFIGURED")), "guardrail_attached()")
        self.assertIn("from models.cached_bedrock import guardrail_attached", self.source)

    def test_the_model_attaches_a_guardrail_through_the_same_predicate(self) -> None:
        model = ast.parse(MODEL.read_text(encoding="utf-8"))
        predicate = ast.unparse(_function(model, "guardrail_attached"))
        self.assertIn("BEDROCK_GUARDRAIL_ENABLED and BEDROCK_GUARDRAIL_ID", predicate)
        self.assertNotIn("BEDROCK_GUARDRAIL_VERSION", predicate)
        config = ast.unparse(_function(model, "_model_config"))
        self.assertIn("if guardrail_attached():", config)
        self.assertNotIn("if BEDROCK_GUARDRAIL_ENABLED", config)

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

    def test_corpus_test_assumes_no_guardrail_version(self) -> None:
        # The deployed runtime pins a published version (#782); a DRAFT default
        # would evaluate a policy users never see.
        corpus = (MODULE.parent / "test_guardrail_corpus_cloud.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("GBAW_BEDROCK_GUARDRAIL_VERSION", "")', corpus)
        self.assertNotIn('"GBAW_BEDROCK_GUARDRAIL_VERSION", "DRAFT"', corpus)

    def test_no_assertion_mentions_the_reply(self) -> None:
        asserts = [node for node in ast.walk(self.tree) if isinstance(node, ast.Assert)]
        self.assertTrue(asserts, "expected assertions in the module")
        for node in asserts:
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            leaked = sorted(names & REPLY_NAMES)
            self.assertEqual(leaked, [], f"line {node.lineno}: assert mentions reply text {leaked}")
            calls = {ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
            self.assertFalse(
                calls & {"str", "run_orchestrator", "_ask"}, f"line {node.lineno}: assert renders the reply"
            )


if __name__ == "__main__":
    unittest.main()
