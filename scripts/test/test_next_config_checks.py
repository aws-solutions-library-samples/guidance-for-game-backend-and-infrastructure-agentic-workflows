"""Behavioral contracts for Next.js development and production headers."""

import json
import os
from pathlib import Path
import subprocess
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_PATH = PROJECT_ROOT / "ui" / "next.config.mjs"


class NextConfigBehaviorTests(unittest.TestCase):
    def _headers(self, node_env: str) -> list[dict]:
        script = (
            f"import config from {json.dumps(CONFIG_PATH.as_uri())};"
            "console.log(JSON.stringify(await config.headers()));"
        )
        env = os.environ.copy()
        env["NODE_ENV"] = node_env
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def _config_keys(self) -> list[str]:
        script = (
            f"import config from {json.dumps(CONFIG_PATH.as_uri())};"
            "console.log(JSON.stringify(Object.keys(config).sort()));"
        )
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_development_does_not_apply_production_nosniff_to_next_assets(self):
        self.assertEqual(self._headers("development"), [])

    def test_production_retains_complete_security_header_set(self):
        rules = self._headers("production")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["source"], "/(.*)")
        headers = {entry["key"]: entry["value"] for entry in rules[0]["headers"]}
        self.assertEqual(
            set(headers),
            {
                "X-Frame-Options",
                "X-Content-Type-Options",
                "Referrer-Policy",
                "Permissions-Policy",
                "Strict-Transport-Security",
                "Content-Security-Policy",
            },
        )
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(headers["Permissions-Policy"], "camera=(), microphone=(), geolocation=()")
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=63072000; includeSubDomains; preload")
        csp = headers["Content-Security-Policy"]
        for directive in (
            "default-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ):
            self.assertIn(directive, csp)

    def test_removed_next_lint_option_is_not_reintroduced(self):
        self.assertNotIn("eslint", self._config_keys())


if __name__ == "__main__":
    unittest.main()
