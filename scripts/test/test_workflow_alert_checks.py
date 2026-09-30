"""Behavioral contracts for scheduled vulnerability alert delivery."""

# Standard library
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "test.yml"


class VulnerabilityAlertWorkflowTests(unittest.TestCase):
    def _alert_job(self) -> str:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        _, alert_and_following_jobs = workflow.split("\n  vulnerability-alert:", maxsplit=1)
        alert_job, _ = alert_and_following_jobs.split("\n  powershell-tests:", maxsplit=1)
        return alert_job

    def test_alert_cli_has_explicit_repository_context_without_checkout(self):
        alert_job = self._alert_job()

        self.assertNotIn("uses: actions/checkout@", alert_job)
        self.assertIn("GH_REPO: ${{ github.repository }}", alert_job)


if __name__ == "__main__":
    unittest.main()
