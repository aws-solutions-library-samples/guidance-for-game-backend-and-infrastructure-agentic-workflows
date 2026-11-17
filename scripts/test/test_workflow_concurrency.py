"""Contract for workflow concurrency groups (#790).

Superseded pull request runs of test.yml and codeql.yml are cancelled, but a
push to main, a scheduled scan or a workflow_dispatch run is never cancelled in
progress, and the release workflow is never cancelled at all.

Standard library only: these tests run before any dependency is installed.
"""

# Standard library
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
CANCELLABLE_WORKFLOWS = ("test.yml", "codeql.yml")
NON_PR_EVENTS = ("push", "schedule", "workflow_dispatch")
EXPRESSION = re.compile(r"\$\{\{\s*([^}]*?)\s*\}\}")


def concurrency_block(path):
    """Return the top-level `concurrency:` mapping as a dict of strings."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("concurrency:")
    except ValueError:
        return None
    block = {}
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            break
        key, _, value = line.strip().partition(":")
        block[key.strip()] = value.strip()
    return block


def context_value(name, ctx):
    """Resolve one GitHub expression operand against a fake context."""
    name = name.strip()
    literal = re.fullmatch(r"'([^']*)'", name)
    if literal:
        return literal.group(1)
    if name not in ctx:
        raise AssertionError(f"unsupported expression operand: {name}")
    return ctx[name]


def evaluate(expression, ctx):
    """Evaluate the small expression subset these workflows may use."""
    if " == " in expression:
        left, right = expression.split(" == ")
        return context_value(left, ctx) == context_value(right, ctx)
    for operand in expression.split("||"):
        value = context_value(operand, ctx)
        if value:
            return value
    return ""


def render(value, ctx):
    """Render a workflow value, expanding each ${{ }} expression."""
    if value in ("true", "false"):
        return value == "true"
    full = EXPRESSION.fullmatch(value)
    if full:
        return evaluate(full.group(1), ctx)
    return EXPRESSION.sub(lambda m: str(evaluate(m.group(1), ctx)), value)


def context(event_name, ref="refs/heads/main", pr_number="", workflow="Test Suite"):
    return {
        "github.workflow": workflow,
        "github.event_name": event_name,
        "github.ref": ref,
        "github.event.pull_request.number": pr_number,
    }


class WorkflowConcurrencyTests(unittest.TestCase):
    def blocks(self):
        for name in CANCELLABLE_WORKFLOWS:
            block = concurrency_block(WORKFLOWS_DIR / name)
            self.assertIsNotNone(block, f"{name} must define top-level concurrency")
            self.assertIn("group", block, name)
            self.assertIn("cancel-in-progress", block, name)
            yield name, block

    def test_cancel_in_progress_only_for_pull_requests(self):
        for name, block in self.blocks():
            cancel = block["cancel-in-progress"]
            with self.subTest(workflow=name, event="pull_request"):
                pr = context("pull_request", ref="refs/pull/7/merge", pr_number="7")
                self.assertIs(render(cancel, pr), True)
            for event in NON_PR_EVENTS:
                with self.subTest(workflow=name, event=event):
                    self.assertIs(render(cancel, context(event)), False)

    def test_group_includes_event_name_and_pr_number_or_ref(self):
        for name, block in self.blocks():
            group = block["group"]
            with self.subTest(workflow=name):
                self.assertIn("github.event_name", group)
                self.assertIn("github.event.pull_request.number", group)
                self.assertIn("github.ref", group)

    def test_main_pushes_never_share_a_group_with_other_events(self):
        for name, block in self.blocks():
            groups = {event: render(block["group"], context(event)) for event in NON_PR_EVENTS}
            with self.subTest(workflow=name):
                self.assertEqual(len(set(groups.values())), len(NON_PR_EVENTS), groups)

    def test_pushes_to_one_pull_request_share_a_group(self):
        for name, block in self.blocks():
            first = render(block["group"], context("pull_request", "refs/pull/7/merge", "7"))
            again = render(block["group"], context("pull_request", "refs/pull/7/merge", "7"))
            other = render(block["group"], context("pull_request", "refs/pull/8/merge", "8"))
            with self.subTest(workflow=name):
                self.assertEqual(first, again)
                self.assertNotEqual(first, other)

    def test_release_workflow_is_never_cancelled(self):
        block = concurrency_block(WORKFLOWS_DIR / "release.yml")
        self.assertIsNotNone(block, "release.yml must define top-level concurrency")
        self.assertEqual(block.get("cancel-in-progress"), "false")


if __name__ == "__main__":
    unittest.main()
