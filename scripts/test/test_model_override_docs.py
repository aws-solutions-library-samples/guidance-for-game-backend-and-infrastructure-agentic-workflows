#!/usr/bin/env python3
"""Static checks for the model-override guidance in the docs and env examples (#556).

Claude Haiku 5.5 and Claude Sonnet 5.5 do not support In-Region invocation, so
a bare ``anthropic.claude-*-5-5`` override fails every request. The override
guidance must name the ``global.`` profile IDs and must not claim that bare
foundation model IDs work for every model.
"""

from pathlib import Path
import re
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OVERRIDE_DOCS = ("README.md", "ui/.env.example", "ui/.env.local.example")


def _normalized(relative_path: str) -> str:
    """Return the file text with comment markers and line wrapping removed."""
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    text = re.sub(r"^#\s?", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


class ModelOverrideDocsTests(unittest.TestCase):
    def test_docs_do_not_claim_bare_foundation_ids_always_work(self):
        for relative_path in _OVERRIDE_DOCS:
            with self.subTest(path=relative_path):
                self.assertNotIn("Foundation model IDs and", _normalized(relative_path))

    def test_docs_require_global_profiles_for_claude_5_5(self):
        for relative_path in _OVERRIDE_DOCS:
            with self.subTest(path=relative_path):
                text = _normalized(relative_path)
                self.assertIn("do not support In-Region invocation", text)
                self.assertIn("global.anthropic.claude-haiku-5-5", text)
                self.assertIn("global.anthropic.claude-sonnet-5-5", text)
                self.assertIn("works only for a model that supports In-Region invocation", text)
                self.assertIn("requires an IAM change first", text)


if __name__ == "__main__":
    unittest.main()
