#!/usr/bin/env python3
"""Static checks for the configurable idle-session warning (#310).

The client idle timer is a UX/local-exposure control, not authorization. These
checks assert only that the idle timeout and warning threshold are deployment
configuration with validated ranges, wired from the frontend runtime template
to the container environment the Next.js app reads via /api/config.
"""

from pathlib import Path
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_TEMPLATE = (
    _REPO_ROOT / "infrastructure/cloudformation/02-frontend-ecs-express.yaml"
).read_text()


class IdleSessionConfigTests(unittest.TestCase):
    def test_idle_timeout_is_bounded_deployment_configuration(self):
        self.assertIn("SessionIdleTimeoutSeconds:", _FRONTEND_TEMPLATE)
        self.assertIn("MinValue: 300", _FRONTEND_TEMPLATE)
        self.assertIn("MaxValue: 28800", _FRONTEND_TEMPLATE)

    def test_idle_warning_is_bounded_deployment_configuration(self):
        self.assertIn("SessionIdleWarningSeconds:", _FRONTEND_TEMPLATE)
        self.assertIn("MinValue: 30", _FRONTEND_TEMPLATE)
        self.assertIn("MaxValue: 600", _FRONTEND_TEMPLATE)

    def test_idle_settings_are_exported_to_the_frontend_runtime(self):
        self.assertIn("Name: GBAW_SESSION_IDLE_TIMEOUT_SECONDS", _FRONTEND_TEMPLATE)
        self.assertIn("Value: !Ref SessionIdleTimeoutSeconds", _FRONTEND_TEMPLATE)
        self.assertIn("Name: GBAW_SESSION_IDLE_WARNING_SECONDS", _FRONTEND_TEMPLATE)
        self.assertIn("Value: !Ref SessionIdleWarningSeconds", _FRONTEND_TEMPLATE)

    def test_documented_defaults_match_the_issue(self):
        # 30-minute idle timeout, 2-minute warning threshold.
        self.assertIn("Default: 1800", _FRONTEND_TEMPLATE)
        self.assertIn("Default: 120", _FRONTEND_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
