#!/usr/bin/env python3
"""Static checks that both deployment builders redact exported trace content (#565).

The shell (`scripts/deploy.sh`) and PowerShell
(`scripts/powershell/Private/New-GameAgentAgentCoreEnvArgs.ps1`) AgentCore
environment builders must pass the same two settings so the hosted runtime
carries no model content in exported spans or OpenTelemetry log records.
"""

from pathlib import Path
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_SH = (_REPO_ROOT / "scripts/deploy.sh").read_text()
_ENV_ARGS_PS1 = (
    _REPO_ROOT / "scripts/powershell/Private/New-GameAgentAgentCoreEnvArgs.ps1"
).read_text()

_CAPTURE_SETTING = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false"
_REDACT_SETTING = "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes="


class TraceContentConfigTests(unittest.TestCase):
    def test_shell_builder_disables_content_capture_and_redacts(self):
        self.assertIn(_CAPTURE_SETTING, _DEPLOY_SH)
        self.assertIn(_REDACT_SETTING, _DEPLOY_SH)

    def test_powershell_builder_disables_content_capture_and_redacts(self):
        self.assertIn(_CAPTURE_SETTING, _ENV_ARGS_PS1)
        self.assertIn(_REDACT_SETTING, _ENV_ARGS_PS1)


if __name__ == "__main__":
    unittest.main()
