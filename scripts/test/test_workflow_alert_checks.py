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

    def test_issue_commands_have_explicit_repository_context_without_checkout(self):
        alert_job = self._alert_job()
        _, issue_step = alert_job.split("- name: Open or update alert issue (scheduled runs)", maxsplit=1)
        issue_commands = [line for line in issue_step.splitlines() if "gh issue " in line]

        self.assertNotIn("uses: actions/checkout@", alert_job)
        self.assertEqual(len(issue_commands), 3)
        for command in issue_commands:
            self.assertIn('--repo "$GITHUB_REPOSITORY"', command)


if __name__ == "__main__":
    unittest.main()
