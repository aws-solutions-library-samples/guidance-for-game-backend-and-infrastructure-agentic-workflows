"""Static and collection contracts for the bounded deployed stress suite."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
STRESS_TEST = PROJECT_ROOT / "backend" / "tests" / "performance" / "test_deployed_health_stress.py"
RUNNER = PROJECT_ROOT / "test-stress.sh"
FULL_RUNNER = PROJECT_ROOT / "scripts" / "test" / "full.sh"
RESOLVER = PROJECT_ROOT / "scripts" / "test" / "resolve-deployment-targets.sh"


class StressSuiteContractTests(unittest.TestCase):
    def test_bounded_deployed_stress_test_exists_and_is_marked(self):
        self.assertTrue(STRESS_TEST.is_file())
        source = STRESS_TEST.read_text(encoding="utf-8")
        self.assertIn("pytest.mark.stress", source)
        self.assertIn("pytest.mark.cloud", source)
        self.assertIn("HEALTH_MAX_REQUESTS", source)
        self.assertIn("RUNTIME_MAX_REQUESTS", source)
        self.assertIn("MAX_WORKERS", source)

    def test_both_runners_use_one_safe_profile_aware_resolver(self):
        runner = RUNNER.read_text(encoding="utf-8")
        full_runner = FULL_RUNNER.read_text(encoding="utf-8")
        resolver = RESOLVER.read_text(encoding="utf-8")

        self.assertIn("source scripts/test/resolve-deployment-targets.sh", full_runner)
        self.assertIn('source "$PROJECT_ROOT/scripts/test/resolve-deployment-targets.sh"', runner)
        self.assertIn("ui/.env.local", resolver)
        self.assertIn("export AWS_PROFILE", resolver)
        self.assertIn("unset FRONTEND_URL", resolver)
        self.assertIn("aws cloudformation describe-stacks", resolver)
        self.assertIn("Refusing unrecognized frontend target", resolver)
        self.assertIn("Refusing invalid AgentCore runtime ID", resolver)
        self.assertIn("Refusing invalid AgentCore runtime ARN", resolver)
        self.assertIn("tests/performance", runner)
        self.assertNotRegex(runner, r"(?m)^\s*eval\b")
        self.assertNotRegex(full_runner, r"(?m)^\s*eval\b")


class DeploymentTargetResolverBehaviorTests(unittest.TestCase):
    def test_profile_fallback_is_exported_to_every_discovery_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            profile_log = root / "profiles.log"
            env_file = root / "env.local"
            env_file.write_text("AWS_PROFILE=fallback-profile\n", encoding="utf-8")
            agentcore_config = root / "agentcore.yaml"
            agentcore_config.write_text("agents: {}\n", encoding="utf-8")

            aws = bin_dir / "aws"
            aws.write_text(
                "#!/bin/bash\n"
                f"printf '%s\\n' \"${{AWS_PROFILE:-}}\" >> {profile_log}\n"
                "if [ \"$1\" = cloudformation ]; then\n"
                "  printf '%s\\n' 'ga-example.ecs.us-west-2.on.aws'\n"
                "else\n"
                "  printf '%s\\n' '{}'\n"
                "fi\n",
                encoding="utf-8",
            )
            aws.chmod(0o755)

            yq = bin_dir / "yq"
            yq.write_text(
                "#!/bin/bash\n"
                "case \"$2\" in\n"
                "  *agent_id*) printf '%s\\n' 'gameagentruntime-AbCdEf1234' ;;\n"
                "  *agent_arn*) printf '%s\\n' 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/gameagentruntime-AbCdEf1234' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            yq.chmod(0o755)

            env = os.environ.copy()
            env.pop("AWS_PROFILE", None)
            env["AWS_REGION"] = "us-west-2"
            env["GBAW_ENV_FILE"] = str(env_file)
            env["GBAW_AGENTCORE_CONFIG_FILE"] = str(agentcore_config)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            command = (
                f"source {RESOLVER}; "
                "printf 'profile=%s\\nfrontend=%s\\nruntime=%s\\n' "
                '"$AWS_PROFILE" "$FRONTEND_URL" "$AGENTCORE_RUNTIME_ID"'
            )
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn("profile=fallback-profile", result.stdout)
            self.assertIn("frontend=https://ga-example.ecs.us-west-2.on.aws", result.stdout)
            self.assertIn("runtime=gameagentruntime-AbCdEf1234", result.stdout)
            profiles = profile_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(profiles)
            self.assertEqual(set(profiles), {"fallback-profile"})



if __name__ == "__main__":
    unittest.main()
