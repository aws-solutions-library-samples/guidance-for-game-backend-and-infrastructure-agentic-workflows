"""Offline checks for the Agones-to-GameLift reference environment (demo/).

Text-level checks pin image versions, ingress, and isolation from the canonical
deployment (YAML-structure checks live in backend/tests/unit/
test_agones_demo_manifests_unit.py, where PyYAML is installed). Behavior
checks run scripts/demo.sh against local fake aws/eksctl/kubectl/helm commands;
nothing here uses credentials, kubeconfig, or cloud resources.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPOSITORY_ROOT / "demo" / "agones-gamelift-migration"
SCRIPT = DEMO / "scripts" / "demo.sh"

class DemoManifestChecks(unittest.TestCase):
    def test_images_and_versions_are_pinned(self) -> None:
        fleet = (DEMO / "agones" / "fleet.yaml").read_text(encoding="utf-8")
        self.assertRegex(fleet, r"simple-game-server:\d+\.\d+\b")
        job = (DEMO / "client" / "load-job.yaml").read_text(encoding="utf-8")
        self.assertRegex(job, r"image: python:\d+\.\d+\.\d+-alpine\d+\.\d+")
        dockerfile = (DEMO / "gamelift" / "server" / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"FROM \S+:\d{4}\.\d+\.\d{8}\.\d+")
        build = (DEMO / "gamelift" / "server" / "build.sh").read_text(encoding="utf-8")
        self.assertRegex(build, r'SDK_SHA256="[0-9a-f]{64}"')
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(script, r'AGONES_CHART_VERSION="\d+\.\d+\.\d+"')
        for name, text in {"fleet": fleet, "job": job, "dockerfile": dockerfile, "build": build}.items():
            self.assertNotRegex(text, r":latest\b", name)

    def test_no_unrestricted_ingress_in_demo_assets(self) -> None:
        allowed = {SCRIPT}  # demo.sh only ever *revokes* 0.0.0.0/0 or rejects it
        for path in sorted((REPOSITORY_ROOT / "demo").rglob("*")):
            if not path.is_file() or path in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotRegex(text, r"CidrIp=0\.0\.0\.0/0|IpRange[\"']?:\s*[\"']?0\.0\.0\.0/0", str(path))
            self.assertNotIn("privateNetworking: false", text, str(path))
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("authorize-security-group-ingress", script)
        self.assertNotRegex(script, r"authorizations[^\n]*0\.0\.0\.0/0")

    def test_demo_is_not_wired_into_the_canonical_deployment(self) -> None:
        for path in [
            REPOSITORY_ROOT / "deploy-all.sh",
            REPOSITORY_ROOT / "scripts" / "deploy.sh",
            REPOSITORY_ROOT / "scripts" / "teardown.sh",
            *sorted((REPOSITORY_ROOT / "infrastructure" / "cloudformation").glob("*.yaml")),
        ]:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("agones-gamelift-migration", text, str(path))
                self.assertNotIn("demo/", text, str(path))

    def test_script_has_valid_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        subprocess.run([bash, "-n", str(SCRIPT)], check=True)


FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state["calls"].append(name + " " + " ".join(args))
state_path.write_text(json.dumps(state))
def out(key, default=""):
    print(state.get(key, default))
joined = " ".join(args)
if name == "aws":
    if args[:2] == ["sts", "get-caller-identity"]:
        out("caller", "arn:aws:sts::123456789012:assumed-role/agones-demo-operator/test")
    elif args[:2] == ["cloudformation", "list-stacks"]:
        prefix = "eksctl-" if "eksctl-" in joined else "gl"
        stacks = state["eksctl_stacks"] if prefix == "eksctl-" else state["gl_stacks"]
        print("\t".join(stacks))
        if prefix == "gl" and state.get("stacks_deleted_on_wait") and state.get("waited"):
            pass
    elif args[:2] == ["cloudformation", "delete-stack"]:
        pass
    elif args[:3] == ["cloudformation", "wait", "stack-delete-complete"]:
        state["gl_stacks"] = []
        state["waited"] = True
        state_path.write_text(json.dumps(state))
    elif args[:2] == ["cloudformation", "describe-stacks"]:
        out("fleet_id", "containerfleet-00000000-0000-0000-0000-000000000000")
    elif args[:2] == ["eks", "describe-cluster"]:
        sys.exit(0 if state["cluster_exists"] else 254)
    elif args[:2] == ["ecr", "describe-repositories"]:
        sys.exit(0 if state["repo_exists"] else 254)
    elif args[:2] == ["ecr", "delete-repository"]:
        state["repo_exists"] = False
        state_path.write_text(json.dumps(state))
    elif args[:2] == ["resourcegroupstaggingapi", "get-resources"]:
        print("\t".join(state["tagged"]))
    elif args[:2] == ["ec2", "describe-nat-gateways"]:
        if "--nat-gateway-ids" in args:
            if state.get("nat_state") == "notfound":
                print("An error occurred (InvalidNatGatewayID.NotFound)", file=sys.stderr)
                sys.exit(254)
            out("nat_state", "available")
        else:
            out("nat_ip", "198.51.100.7")
    elif args[:2] == ["ec2", "describe-instances"]:
        out("instance_state", "running")
    elif args[:2] == ["ec2", "describe-volumes"]:
        out("volume_state", "available")
    elif args[:2] == ["gamelift", "describe-container-fleet"]:
        print("4192\t4196")
elif name == "eksctl":
    if args[:2] == ["delete", "cluster"]:
        state["cluster_exists"] = False
        state["eksctl_stacks"] = []
        state["tagged"] = []
        state_path.write_text(json.dumps(state))
    elif args[:1] == ["version"]:
        print("0.231.0")
elif name == "kubectl":
    if args[:2] == ["get", "ns"]:
        sys.exit(0 if state.get("agones_installed") else 1)
'''


class DemoScriptBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("aws", "eksctl", "kubectl", "helm", "jq"):
            path = bin_dir / tool
            path.write_text(FAKE, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.state = self.tmp / "state.json"
        self.write_state()
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp),
            "FAKE_STATE": str(self.state),
            "AWS_REGION": "us-east-1",
            "CLUSTER_NAME": "agones-demo",
            "STACK_PREFIX": "gl-demo",
            "VERIFY_ATTEMPTS": "2",
            "VERIFY_DELAY_SECONDS": "0",
        }

    def write_state(self, **overrides) -> None:
        state = {
            "calls": [],
            "gl_stacks": ["gl-demo-migration", "gl-demo-unreal"],
            "eksctl_stacks": ["eksctl-agones-demo-cluster"],
            "cluster_exists": True,
            "repo_exists": True,
            "tagged": ["arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0example"],
            "agones_installed": False,
        }
        state.update(overrides)
        self.state.write_text(json.dumps(state))

    def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env={**self.env, **env}, capture_output=True, text=True, timeout=60
        )

    def calls(self) -> list[str]:
        return json.loads(self.state.read_text())["calls"]

    def test_cluster_up_rejects_unrestricted_or_missing_operator_cidr(self) -> None:
        for cidr in ("", "0.0.0.0/0", "203.0.113.0/16", "not-a-cidr"):
            result = self.run_script("cluster-up", OPERATOR_CIDR=cidr, OWNER="tester")
            self.assertNotEqual(result.returncode, 0, cidr)
            self.assertFalse(any(c.startswith("eksctl create") for c in self.calls()), cidr)

    def test_cluster_up_renders_every_placeholder(self) -> None:
        capture = self.tmp / "rendered.yaml"
        fake_eksctl = self.tmp / "bin" / "eksctl"
        fake_eksctl.write_text(
            FAKE.replace(
                'elif args[:1] == ["version"]:',
                'elif args[:2] == ["create", "cluster"]:\n'
                f'        Path({str(capture)!r}).write_text(Path(args[3]).read_text())\n'
                '    elif args[:1] == ["version"]:',
            ),
            encoding="utf-8",
        )
        result = self.run_script("cluster-up", OPERATOR_CIDR="203.0.113.10/32", OWNER="tester")
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = capture.read_text(encoding="utf-8")
        self.assertNotIn("__", rendered)
        self.assertIn("- 203.0.113.10/32", rendered)
        self.assertIn("owner: tester", rendered)
        self.assertRegex(rendered, r"expires-at: \d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z")

    def test_down_deletes_in_order_and_verifies(self) -> None:
        result = self.run_script("down")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()

        def first(prefix: str) -> int:
            return next(i for i, c in enumerate(calls) if c.startswith(prefix))

        self.assertLess(first("aws cloudformation delete-stack"), first("eksctl delete cluster"))
        self.assertLess(first("aws cloudformation wait stack-delete-complete"), first("eksctl delete cluster"))
        self.assertLess(first("eksctl delete cluster"), first("aws ecr delete-repository"))
        self.assertLess(first("aws ecr delete-repository"), first("aws resourcegroupstaggingapi get-resources"))
        deleted = [c for c in calls if c.startswith("aws cloudformation delete-stack")]
        self.assertEqual(len(deleted), 2)
        self.assertTrue(all("--stack-name gl-demo-" in c for c in deleted))
        self.assertIn("No demo resources remain", result.stdout)
        tag_call = next(c for c in calls if c.startswith("aws resourcegroupstaggingapi"))
        self.assertIn("Key=demo-env,Values=agones-demo", tag_call)

    def test_down_never_weakens_protections(self) -> None:
        self.run_script("down")
        for call in self.calls():
            self.assertNotRegex(
                call, r"termination-protection|deletion-protection|retention|put-bucket-versioning|--retain", call
            )

    def test_verify_clean_fails_when_anything_remains(self) -> None:
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("natgateway/nat-0example", result.stdout)
        self.assertIn("gl-demo-migration", result.stdout)
        self.assertIn("Cluster remains", result.stdout)

    def test_verify_clean_ignores_nat_gateways_the_tagging_api_still_lists_after_deletion(self) -> None:
        self.write_state(gl_stacks=[], eksctl_stacks=[], cluster_exists=False, repo_exists=False, nat_state="deleted")
        result = self.run_script("verify-clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verify_clean_ignores_stale_tag_listings_for_gone_ec2_resources(self) -> None:
        stale = [
            "arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0gone",
            "arn:aws:ec2:us-east-1:123456789012:instance/i-0gone",
            "arn:aws:ec2:us-east-1:123456789012:volume/vol-0gone",
        ]
        self.write_state(
            gl_stacks=[], eksctl_stacks=[], cluster_exists=False, repo_exists=False,
            tagged=stale, nat_state="notfound", instance_state="terminated", volume_state="None",
        )
        result = self.run_script("verify-clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verify_clean_reports_ec2_resources_that_still_exist(self) -> None:
        self.write_state(
            gl_stacks=[], eksctl_stacks=[], cluster_exists=False, repo_exists=False,
            tagged=["arn:aws:ec2:us-east-1:123456789012:volume/vol-0still"], volume_state="in-use",
        )
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vol-0still", result.stdout)

    def test_verify_clean_retries_before_reporting(self) -> None:
        self.run_script("verify-clean")
        tag_calls = [c for c in self.calls() if c.startswith("aws resourcegroupstaggingapi")]
        self.assertEqual(len(tag_calls), 2)

    def test_verify_clean_passes_when_empty(self) -> None:
        self.write_state(gl_stacks=[], eksctl_stacks=[], cluster_exists=False, repo_exists=False, tagged=[])
        result = self.run_script("verify-clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_restrict_fleet_ingress_replaces_open_rule_with_nat_address(self) -> None:
        result = self.run_script("restrict-fleet-ingress", "gl-demo-migration")
        self.assertEqual(result.returncode, 0, result.stderr)
        update = next(c for c in self.calls() if c.startswith("aws gamelift update-container-fleet"))
        auth = re.search(r"--instance-inbound-permission-authorizations (\[.*?\]) --", update).group(1)
        revoke = re.search(r"--instance-inbound-permission-revocations (\[.*?\])", update).group(1)
        self.assertEqual(json.loads(auth), [{"FromPort": 4192, "ToPort": 4196, "IpRange": "198.51.100.7/32", "Protocol": "UDP"}])
        self.assertEqual(json.loads(revoke)[0]["IpRange"], "0.0.0.0/0")

    def test_restrict_fleet_ingress_only_touches_demo_stacks(self) -> None:
        result = self.run_script("restrict-fleet-ingress", "production-fleet")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("update-container-fleet" in c for c in self.calls()))

    def test_load_rejects_non_ip_targets(self) -> None:
        result = self.run_script("load", "$(touch /tmp/x)", "7654")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c.startswith("kubectl apply") for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
