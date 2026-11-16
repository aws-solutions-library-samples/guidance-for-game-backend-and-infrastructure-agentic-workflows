"""Parity checks for the AWS Guidance Solution ID tracking stack.

AWS Guidance deployments are counted from a CloudFormation stack whose
description carries the Guidance ID. Both supported deployment paths, bash
(``scripts/deploy.sh``) and PowerShell (``Deploy-GameAgent``), must deploy that
stack before the base infrastructure, and both teardown paths must delete it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_ROOT / "infrastructure" / "cloudformation" / "00-solution-tracking.yaml"
BASH_DEPLOY = PROJECT_ROOT / "scripts" / "deploy.sh"
BASH_TEARDOWN = PROJECT_ROOT / "scripts" / "teardown.sh"
POWERSHELL_DEPLOY = PROJECT_ROOT / "scripts" / "powershell" / "Public" / "Deploy-GameAgent.ps1"
POWERSHELL_REMOVE = PROJECT_ROOT / "scripts" / "powershell" / "Public" / "Remove-GameAgent.ps1"

GUIDANCE_ID = "SO9693"


def _first_index(text: str, pattern: str, label: str) -> int:
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError(f"{label}: pattern not found: {pattern}")
    return match.start()


class SolutionTrackingTemplateTest(unittest.TestCase):
    def test_template_description_carries_guidance_id(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        description = re.search(r"^Description:\s*[\"']?(.+?)[\"']?\s*$", text, re.MULTILINE)
        self.assertIsNotNone(description, "tracking template has no top-level Description")
        self.assertIn(f"({GUIDANCE_ID})", description.group(1))


class SolutionTrackingDeployParityTest(unittest.TestCase):
    def test_bash_deploy_creates_tracking_stack_before_base_infrastructure(self) -> None:
        text = BASH_DEPLOY.read_text(encoding="utf-8")
        tracking = _first_index(text, r"00-solution-tracking\.yaml", "deploy.sh")
        self.assertRegex(text, r'--stack-name "\$\{PROJECT_NAME\}-solution-tracking"')
        base = _first_index(text, r"01-base-infrastructure\.yaml", "deploy.sh")
        self.assertLess(tracking, base)

    def test_powershell_deploy_creates_tracking_stack_before_base_infrastructure(self) -> None:
        text = POWERSHELL_DEPLOY.read_text(encoding="utf-8")
        tracking = _first_index(
            text,
            r'Deploy-Stack -StackName "\$ProjectName-solution-tracking"\s*`\s*'
            r"-TemplateFile \(Join-Path \$infraPath '00-solution-tracking\.yaml'\)",
            "Deploy-GameAgent.ps1",
        )
        base = _first_index(text, r'Deploy-Stack -StackName "\$ProjectName-infrastructure"', "Deploy-GameAgent.ps1")
        self.assertLess(tracking, base)


class SolutionTrackingTeardownParityTest(unittest.TestCase):
    def test_bash_teardown_deletes_tracking_stack(self) -> None:
        text = BASH_TEARDOWN.read_text(encoding="utf-8")
        self.assertRegex(text, r"delete-stack --stack-name \$\{PROJECT_NAME\}-solution-tracking")

    def test_powershell_remove_deletes_tracking_stack(self) -> None:
        text = POWERSHELL_REMOVE.read_text(encoding="utf-8")
        self.assertRegex(text, r'Remove-Stack "\$ProjectName-solution-tracking"')


if __name__ == "__main__":
    unittest.main()
