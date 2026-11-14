"""Behavior checks for consumers of the frontend stack's ServiceUrl output (issue #641).

The ECS Express ``ServiceUrl`` output (``!GetAtt FrontendService.Endpoint``) has
been returned both as a bare host and with its ``https://`` scheme. Every shell
consumer must use ``https://<host>`` exactly once for either form, and the
deployed-test resolver must still refuse anything that is not an ECS Express host
in the deployment Region. The PowerShell consumers share one normalizer, whose
behavior the Pester suite covers. All identifiers are synthetic.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOLVER = PROJECT_ROOT / "scripts" / "test" / "resolve-deployment-targets.sh"
CHECK_DEPLOYMENT = PROJECT_ROOT / "scripts" / "infrastructure" / "check-deployment.sh"
VALIDATE_DEPLOYMENT = PROJECT_ROOT / "validate-deployment.sh"
POWERSHELL_ROOT = PROJECT_ROOT / "scripts" / "powershell"
POWERSHELL_HELPER = POWERSHELL_ROOT / "Private" / "ConvertTo-GameAgentFrontendUrl.ps1"
POWERSHELL_CONSUMERS = (
    POWERSHELL_ROOT / "Public" / "Deploy-GameAgent.ps1",
    POWERSHELL_ROOT / "Public" / "Get-GameAgentStatus.ps1",
    POWERSHELL_ROOT / "Public" / "Test-GameAgentFull.ps1",
)

HOST = "ga-example.ecs.us-west-2.on.aws"
EXPECTED_URL = f"https://{HOST}"
# The two observed output forms, plus a trailing slash and a plain-HTTP scheme.
SERVICE_URL_FORMS = (HOST, f"https://{HOST}", f"https://{HOST}/", f"http://{HOST}")
RUNTIME_ID = "gameagentruntime-AbCdEf1234"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{RUNTIME_ID}"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# Offline aws stub. CloudFormation output reads answer by --query, and the
# frontend ServiceUrl comes from STUB_SERVICE_URL. Every other call succeeds.
AWS_STUB = """#!/bin/bash
query=""
previous=""
for argument in "$@"; do
    if [ "$previous" = "--query" ]; then
        query="$argument"
    fi
    previous="$argument"
done
if [ "$1 $2" = "cloudformation describe-stacks" ]; then
    case "$query" in
        *ServiceUrl*) printf '%s\\n' "$STUB_SERVICE_URL" ;;
        *StackStatus*) printf '%s\\n' CREATE_COMPLETE ;;
        *UserPoolId*) printf '%s\\n' us-west-2_EXAMPLE ;;
        *GuardrailId*) printf '%s\\n' gr-example0000 ;;
        *GuardrailVersion*) printf '%s\\n' 1 ;;
        *) printf '%s\\n' None ;;
    esac
    exit 0
fi
printf '%s\\n' '{}'
"""

YQ_STUB = f"""#!/bin/bash
case "$2" in
    *agent_id*) printf '%s\\n' '{RUNTIME_ID}' ;;
    *agent_arn*) printf '%s\\n' '{RUNTIME_ARN}' ;;
esac
"""

# Records every URL it is asked to fetch and reports success.
CURL_STUB = """#!/bin/bash
for argument in "$@"; do
    case "$argument" in
        -*) ;;
        *) printf '%s\\n' "$argument" >> "$STUB_CURL_LOG" ;;
    esac
done
exit 0
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class _StubbedAwsTestCase(unittest.TestCase):
    """Runs a script with offline aws, yq, and curl stubs ahead of PATH."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="gbaw-service-url-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        _write_executable(self.bin_dir / "aws", AWS_STUB)
        _write_executable(self.bin_dir / "yq", YQ_STUB)
        _write_executable(self.bin_dir / "curl", CURL_STUB)
        self.curl_log = self.root / "curl.log"
        self.agentcore_config = self.root / "agentcore.yaml"
        self.agentcore_config.write_text("agents: {}\n", encoding="utf-8")

    def run_bash(self, command: str, service_url: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "AWS_PROFILE": "example-profile",
                "AWS_REGION": "us-west-2",
                "GBAW_AGENTCORE_CONFIG_FILE": str(self.agentcore_config),
                "GBAW_ENV_FILE": str(self.root / "missing.env"),
                "STUB_SERVICE_URL": service_url,
                "STUB_CURL_LOG": str(self.curl_log),
            }
        )
        return subprocess.run(
            ["bash", "-c", command],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


class DeploymentTargetResolverTests(_StubbedAwsTestCase):
    def resolve(self, service_url: str) -> subprocess.CompletedProcess[str]:
        command = (
            f'if source "{RESOLVER}"; then echo resolved=yes; else echo resolved=no; fi; '
            'printf "frontend=%s\\n" "${FRONTEND_URL-<unset>}"'
        )
        return self.run_bash(command, service_url, PROJECT_ROOT)

    def test_accepts_the_service_url_with_or_without_its_scheme(self) -> None:
        for service_url in SERVICE_URL_FORMS:
            with self.subTest(service_url=service_url):
                result = self.resolve(service_url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("resolved=yes", result.stdout, result.stderr)
                self.assertIn(f"frontend={EXPECTED_URL}\n", result.stdout)
                self.assertEqual(result.stdout.count("https://"), 1, result.stdout)

    def test_still_refuses_a_host_that_is_not_ecs_express_in_the_region(self) -> None:
        for service_url in (
            "https://ga-example.ecs.us-east-1.on.aws",
            "ga-example.ecs.us-east-1.on.aws",
            "https://example.com",
            "https://example.com/ga-example.ecs.us-west-2.on.aws",
            "https://ga-example.ecs.us-west-2.on.aws.example.com",
            "https://ga-example.ecs.us-west-2.on.aws/path",
            "https://https://ga-example.ecs.us-west-2.on.aws",
            "ftp://ga-example.ecs.us-west-2.on.aws",
            "None",
            "",
        ):
            with self.subTest(service_url=service_url):
                result = self.resolve(service_url)
                self.assertIn("resolved=no", result.stdout)
                self.assertIn("frontend=<unset>", result.stdout)
                self.assertIn("Refusing unrecognized frontend target", result.stderr)


class CheckDeploymentUrlsTests(_StubbedAwsTestCase):
    def test_urls_report_one_scheme_for_either_service_url_form(self) -> None:
        for service_url in SERVICE_URL_FORMS:
            with self.subTest(service_url=service_url):
                result = self.run_bash(f'"{CHECK_DEPLOYMENT}" urls', service_url, self.root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"FRONTEND_URL={EXPECTED_URL}\n", result.stdout)
                self.assertEqual(result.stdout.count("https://"), 1, result.stdout)


class ValidateDeploymentHealthCheckTests(_StubbedAwsTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Run the real script from a throwaway tree so its sibling exporter
        # check is a no-op and the relative backend/ state is synthetic.
        self.tree = self.root / "repo"
        (self.tree / "scripts" / "infrastructure").mkdir(parents=True)
        (self.tree / "backend").mkdir()
        script = self.tree / "validate-deployment.sh"
        script.write_text(VALIDATE_DEPLOYMENT.read_text(encoding="utf-8"), encoding="utf-8")
        script.chmod(0o755)
        _write_executable(
            self.tree / "scripts" / "infrastructure" / "check-exporter-auth.sh",
            "#!/bin/bash\necho 'ADOT exporter auth: OK'\n",
        )
        (self.tree / "backend" / ".bedrock_agentcore.yaml").write_text("agents: {}\n", encoding="utf-8")
        (self.tree / "backend" / ".env.local").write_text(
            "GBAW_GAMELIFT_KB_ID=EXAMPLEKB01\nGBAW_EKS_KB_ID=EXAMPLEKB02\nGBAW_COST_KB_ID=EXAMPLEKB03\n",
            encoding="utf-8",
        )

    def test_health_check_requests_one_scheme_for_either_service_url_form(self) -> None:
        for service_url in SERVICE_URL_FORMS:
            with self.subTest(service_url=service_url):
                self.curl_log.write_text("", encoding="utf-8")
                result = self.run_bash("./validate-deployment.sh", service_url, self.tree)
                output = ANSI_ESCAPE.sub("", result.stdout)
                self.assertEqual(result.returncode, 0, output + result.stderr)
                self.assertIn("PASS Health endpoint", output)
                self.assertEqual(
                    self.curl_log.read_text(encoding="utf-8").splitlines(),
                    [f"{EXPECTED_URL}/api/health"],
                )


class PowerShellConsumerTests(unittest.TestCase):
    def test_powershell_consumers_share_the_frontend_url_normalizer(self) -> None:
        helper = POWERSHELL_HELPER.read_text(encoding="utf-8")
        self.assertIn("function ConvertTo-GameAgentFrontendUrl", helper)
        for consumer in POWERSHELL_CONSUMERS:
            with self.subTest(consumer=consumer.name):
                text = consumer.read_text(encoding="utf-8")
                self.assertIn("ConvertTo-GameAgentFrontendUrl -ServiceUrl", text)
                self.assertNotRegex(text, r"https://\$\{?frontendUrl")


if __name__ == "__main__":
    unittest.main()
