#!/usr/bin/env python3
"""Static checks for configurable Cognito sliding-session lifetimes (#309)."""

from pathlib import Path
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_TEMPLATE = (_REPO_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml").read_text()
_FRONTEND_TEMPLATE = (_REPO_ROOT / "infrastructure/cloudformation/02-frontend-ecs-express.yaml").read_text()


class SessionLifetimeConfigTests(unittest.TestCase):
    def test_cognito_refresh_token_uses_absolute_lifetime_parameter(self):
        self.assertIn("SessionAbsoluteLifetimeHours:", _BASE_TEMPLATE)
        self.assertIn("RefreshTokenValidity: !Ref SessionAbsoluteLifetimeHours", _BASE_TEMPLATE)
        self.assertIn("RefreshToken: hours", _BASE_TEMPLATE)
        self.assertIn("AccessToken: minutes", _BASE_TEMPLATE)
        self.assertIn("IdToken: minutes", _BASE_TEMPLATE)

    def test_absolute_lifetime_is_exported_to_frontend_runtime(self):
        self.assertIn("${ProjectName}-SessionAbsoluteLifetimeHours", _BASE_TEMPLATE)
        self.assertIn("Name: GBAW_SESSION_ABSOLUTE_LIFETIME_HOURS", _FRONTEND_TEMPLATE)
        self.assertIn(
            "Fn::ImportValue: !Sub '${ProjectName}-SessionAbsoluteLifetimeHours'",
            _FRONTEND_TEMPLATE,
        )

    def test_idle_refresh_window_is_deployment_configuration(self):
        self.assertIn("SessionIdleRefreshSeconds:", _FRONTEND_TEMPLATE)
        self.assertIn("Name: GBAW_SESSION_IDLE_REFRESH_SECONDS", _FRONTEND_TEMPLATE)
        self.assertIn("Value: !Ref SessionIdleRefreshSeconds", _FRONTEND_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
