"""Offline checks for the Agones-to-GameLift reference environment (demo/).

Text-level checks pin image versions, ingress, and isolation from the canonical
deployment (YAML-structure checks live in backend/tests/unit/
test_agones_demo_manifests_unit.py, where PyYAML is installed). Behavior
checks run scripts/demo.sh against local fake aws/eksctl/kubectl/helm commands
and the real jq; nothing here uses credentials, kubeconfig, or cloud resources.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPOSITORY_ROOT / "demo" / "agones-gamelift-migration"
SCRIPT = DEMO / "scripts" / "demo.sh"
DEMO_TESTS = {
    "scripts/test/test_agones_gamelift_demo.py",
    "backend/tests/unit/test_agones_demo_manifests_unit.py",
}
# A license inventory names the vendored files; it wires nothing.
DEMO_REFERENCE_ALLOWED = {"THIRD-PARTY-LICENSES"}
JQ = shutil.which("jq")

ENV = "agones-demo"
DEMO_TAGS = {"demo": "agones-gamelift-migration", "demo-env": ENV}
OTHER_ENV_TAGS = {"demo": "agones-gamelift-migration", "demo-env": "other-env"}
ENDPOINT = "https://EXAMPLE0123456789.gr7.us-east-1.eks.amazonaws.com"
OPEN_RULE = {"FromPort": 4192, "ToPort": 4196, "IpRange": "0.0.0.0/0", "Protocol": "UDP"}
NAT_RULE = {"FromPort": 4192, "ToPort": 4196, "IpRange": "198.51.100.7/32", "Protocol": "UDP"}


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPOSITORY_ROOT, capture_output=True, check=True
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


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
        allowed = {SCRIPT}  # demo.sh only ever revokes 0.0.0.0/0 or rejects it
        for path in sorted((REPOSITORY_ROOT / "demo").rglob("*")):
            if not path.is_file() or path in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotRegex(text, r"CidrIp=0\.0\.0\.0/0|IpRange[\"']?:\s*[\"']?0\.0\.0\.0/0", str(path))
            self.assertNotRegex(text, r"IpRange[\"']?:\s*[\"']?::/0", str(path))
            self.assertNotIn("privateNetworking: false", text, str(path))
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("authorize-security-group-ingress", script)
        self.assertNotRegex(script, r"authorizations[^\n]*0\.0\.0\.0/0")

    def test_demo_is_not_referenced_outside_demo(self) -> None:
        tracked = _tracked_files()
        for entry_point in (
            "deploy-all.sh",
            "scripts/deploy.sh",
            "scripts/teardown.sh",
            "scripts/infrastructure/deploy-kb.sh",
            "scripts/powershell/Public/Deploy-GameAgent.ps1",
        ):
            self.assertIn(entry_point, tracked, f"entry point moved or renamed: {entry_point}")
        pattern = re.compile(r"agones-gamelift-migration|demo/agones")
        scanned = 0
        for name in tracked:
            if name.startswith("demo/") or name in DEMO_TESTS | DEMO_REFERENCE_ALLOWED:
                continue
            path = REPOSITORY_ROOT / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(pattern.search(text), f"{name} references the demo")
            scanned += 1
        self.assertGreater(scanned, 100)

    def test_generated_template_checklist_requires_narrow_inbound_permissions(self) -> None:
        readme = (DEMO / "gamelift" / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, r"InstanceInboundPermissions")
        self.assertRegex(readme, r"never `0\.0\.0\.0/0` or `::/0`")
        self.assertRegex(readme, r"PermissionsBoundary")

    def test_runbook_links_resolve_and_build_script_is_executable(self) -> None:
        for readme in (DEMO / "README.md", DEMO / "gamelift" / "README.md"):
            text = readme.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)#:]+)\)", text):
                self.assertTrue((readme.parent / target).exists(), f"{readme.name}: broken link {target}")
        build = DEMO / "gamelift" / "server" / "build.sh"
        self.assertTrue(build.stat().st_mode & stat.S_IXUSR, "build.sh must be executable")

    def test_vendored_wrapper_carries_its_license(self) -> None:
        wrapper = DEMO / "gamelift" / "server" / "wrapper-src"
        self.assertIn("Apache License", (wrapper / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("Amazon.com, Inc. or its affiliates", (wrapper / "NOTICE").read_text(encoding="utf-8"))
        inventory = (REPOSITORY_ROOT / "THIRD-PARTY-LICENSES").read_text(encoding="utf-8")
        self.assertRegex(inventory, r"amazon-gamelift-toolkit SdkGoWrapper\s+Apache-2\.0")

    def test_script_has_valid_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        subprocess.run([bash, "-n", str(SCRIPT)], check=True)


# One fake for aws, eksctl, kubectl, and helm. It keeps resource state in a JSON
# file, applies the parts of each --query that demo.sh relies on (stack-name
# prefix and status filters included), and fails like the real CLI: not-found
# errors and injected errors exit 254 with the service message on stderr.
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
joined = name + " " + " ".join(args)
record = joined
if name in ("kubectl", "helm"):
    record = "KUBECONFIG=" + os.environ.get("KUBECONFIG", "") + " " + joined
state["calls"].append(record)

def save():
    state_path.write_text(json.dumps(state))

def fail(message):
    save()
    print("An error occurred " + message, file=sys.stderr)
    sys.exit(254)

def opt(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default

def tag_list(tags):
    return [{"Key": k, "Value": v} for k, v in tags.items()]

for prefix, message in state.get("errors", {}).items():
    if joined.startswith(prefix):
        fail(message)

query = opt("--query", "")
stacks = state["stacks"]
if name == "aws":
    service, op = args[0], args[1]
    if (service, op) == ("sts", "get-caller-identity"):
        arn = "arn:aws:sts::%s:assumed-role/agones-demo-operator/test" % state["account"]
        print({"[Account,Arn]": state["account"] + "\t" + arn, "Account": state["account"]}.get(query, arn))
    elif (service, op) == ("iam", "get-policy"):
        if not state["boundary_exists"]:
            fail("(NoSuchEntity) when calling the GetPolicy operation: Policy does not exist")
        print("{}")
    elif (service, op) == ("cloudformation", "list-stacks"):
        # Only the exact filter demo.sh is meant to use: every status except
        # DELETE_COMPLETE, names with a non-empty prefix. Anything else fails,
        # so a widened or narrowed filter cannot pass silently.
        match = re.fullmatch(
            r"StackSummaries\[\?StackStatus!='DELETE_COMPLETE' && starts_with\(StackName, '([a-z][a-z0-9-]+-)'\)\]"
            r"\.\[StackName,StackStatus\]",
            query,
        )
        if not match or "--stack-status-filter" in args:
            fail("(FakeUnsupportedQuery) list-stacks query not recognized: " + query)
        for stack_name, stack in stacks.items():
            if stack_name.startswith(match.group(1)) and stack["status"] != "DELETE_COMPLETE":
                print(stack_name + "\t" + stack["status"])
    elif (service, op) == ("cloudformation", "describe-stacks"):
        stack_name = opt("--stack-name")
        stack = stacks.get(stack_name)
        if stack is None or stack["status"] == "DELETE_COMPLETE":
            fail("(ValidationError) when calling the DescribeStacks operation: Stack with id %s does not exist" % stack_name)
        if "Tags" in query:
            print(json.dumps(tag_list(stack.get("tags", {}))))
        elif "StackStatus" in query:
            print(stack["status"])
            if stack.get("settle_after") is not None:
                stack["settle_after"] -= 1
                if stack["settle_after"] <= 0:
                    stack["status"] = stack["status"].replace("_IN_PROGRESS", "_COMPLETE")
                    stack["settle_after"] = None
        elif "FleetId" in query:
            print(stack.get("fleet_id", "None"))
    elif (service, op) == ("cloudformation", "list-change-sets"):
        stack = stacks[opt("--stack-name")]
        ids = [f"{opt('--stack-name')}/{i}" for i in range(len(stack.get("change_sets", [])))]
        print("\t".join(ids) if ids else "None")
    elif (service, op) == ("cloudformation", "describe-change-set"):
        stack_name, index = opt("--change-set-name").rsplit("/", 1)
        print(json.dumps(tag_list(stacks[stack_name]["change_sets"][int(index)])))
    elif (service, op) == ("cloudformation", "describe-stack-resources"):
        stack = stacks[opt("--stack-name")]
        print("\t".join(stack.get("fleets", [])) if stack.get("fleets") else "None")
    elif (service, op) == ("logs", "delete-log-group"):
        group = opt("--log-group-name")
        if group not in state.setdefault("log_groups", []):
            fail("(ResourceNotFoundException) when calling the DeleteLogGroup operation: The specified log group does not exist.")
        state["log_groups"].remove(group)
    elif (service, op) == ("logs", "describe-log-groups"):
        prefix = opt("--log-group-name-prefix", "")
        found = [g for g in state.setdefault("log_groups", []) if g.startswith(prefix)]
        print("\t".join(found) if found else "None")
    elif (service, op) == ("cloudformation", "delete-stack"):
        stacks[opt("--stack-name")]["status"] = "DELETE_IN_PROGRESS"
    elif (service, op) == ("cloudformation", "wait"):
        stacks[opt("--stack-name")]["status"] = "DELETE_COMPLETE"
    elif (service, op) == ("eks", "describe-cluster"):
        cluster = state["cluster"]
        if cluster is None:
            fail("(ResourceNotFoundException) when calling the DescribeCluster operation: No cluster found")
        print(json.dumps(cluster["tags"]) if query == "cluster.tags" else cluster["endpoint"])
    elif (service, op) == ("eks", "update-kubeconfig"):
        server = state.get("kube_server") or state["cluster"]["endpoint"]
        Path(opt("--kubeconfig")).write_text(json.dumps({"server": server}))
    elif (service, op) == ("eks", "describe-access-entry"):
        print(json.dumps(state["access_groups"]))
    elif (service, op) == ("ecr", "describe-repositories"):
        if state["repo"] is None:
            fail("(RepositoryNotFoundException) when calling the DescribeRepositories operation")
        print("arn:aws:ecr:us-east-1:%s:repository/agones-gamelift-demo/game-server" % state["account"])
    elif (service, op) == ("ecr", "list-tags-for-resource"):
        print(json.dumps(tag_list(state["repo"]["tags"])))
    elif (service, op) == ("ecr", "delete-repository"):
        state["repo"] = None
    elif (service, op) == ("resourcegroupstaggingapi", "get-resources"):
        print("\t".join(state["tagged"]) if state["tagged"] else "None")
    elif (service, op) == ("ec2", "describe-nat-gateways"):
        if "--nat-gateway-ids" in args:
            if state.get("nat_state") == "notfound":
                fail("(InvalidNatGatewayID.NotFound)")
            print(state.get("nat_state", "available"))
        else:
            print(state.get("nat_ip", "198.51.100.7"))
    elif (service, op) == ("ec2", "describe-instances"):
        print(state.get("instance_state", "running"))
    elif (service, op) == ("ec2", "describe-volumes"):
        print(state.get("volume_state", "available"))
    elif (service, op) == ("gamelift", "describe-container-fleet"):
        if "InstanceConnectionPortRange" in query:
            print("\t".join(str(p) for p in state["fleet_ports"]))
        else:
            print(json.dumps(state["permissions"]))
    elif (service, op) == ("gamelift", "update-container-fleet"):
        if not state.get("update_ignored"):
            revoke = json.loads(opt("--instance-inbound-permission-revocations", "[]"))
            add = json.loads(opt("--instance-inbound-permission-authorizations", "[]"))
            state["permissions"] = [p for p in state["permissions"] if p not in revoke] + add
elif name == "eksctl":
    if args[:2] == ["delete", "cluster"]:
        state["cluster"] = None
        state["eksctl_deleted"] = True
        for stack_name in list(stacks):
            if stack_name.startswith("eksctl-"):
                stacks[stack_name]["status"] = "DELETE_COMPLETE"
        state["tagged"] = []
    elif args[:2] == ["create", "cluster"]:
        if state.get("capture"):
            Path(state["capture"]).write_text(Path(opt("-f")).read_text())
        state["created_kubeconfig"] = opt("--kubeconfig")
    elif args[:1] == ["version"]:
        print("0.231.0")
elif name == "kubectl":
    if args[:2] == ["config", "view"]:
        print(json.loads(Path(os.environ["KUBECONFIG"]).read_text())["server"])
    elif args[:2] == ["get", "ns"]:
        save()
        sys.exit(0 if state.get("agones_installed") else 1)
    elif args[:1] == ["apply"] and "-" in args:
        state.setdefault("applied", []).append(sys.stdin.read())
save()
'''


@unittest.skipUnless(JQ, "jq is required for the demo.sh behavior checks")
class DemoScriptBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("aws", "eksctl", "kubectl", "helm"):
            path = bin_dir / tool
            path.write_text(FAKE, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        (bin_dir / "jq").symlink_to(JQ)
        self.kubeconfig = self.tmp / ".kube" / f"agones-demo-{ENV}"
        self.state = self.tmp / "state.json"
        self.write_state()
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp),
            "FAKE_STATE": str(self.state),
            "AWS_REGION": "us-east-1",
            "CLUSTER_NAME": ENV,
            "STACK_PREFIX": "gl-demo",
            "VERIFY_ATTEMPTS": "2",
            "VERIFY_DELAY_SECONDS": "0",
            "POLL_SECONDS": "0",
        }

    def write_state(self, **overrides) -> None:
        state = {
            "calls": [],
            "account": "123456789012",
            "boundary_exists": True,
            "stacks": {
                "gl-demo-migration": {"status": "CREATE_COMPLETE", "tags": DEMO_TAGS,
                                      "fleet_id": "containerfleet-00000000-0000-0000-0000-000000000000"},
                "gl-demo-unreal": {"status": "UPDATE_ROLLBACK_COMPLETE", "tags": DEMO_TAGS},
                "gl-demo-colleague": {"status": "CREATE_COMPLETE", "tags": OTHER_ENV_TAGS},
                "gl-demo-untagged": {"status": "CREATE_COMPLETE", "tags": {}},
                "gl-demo-old": {"status": "DELETE_COMPLETE", "tags": DEMO_TAGS},
                "production-fleet": {"status": "CREATE_COMPLETE", "tags": DEMO_TAGS},
                f"eksctl-{ENV}-cluster": {"status": "CREATE_COMPLETE", "tags": DEMO_TAGS},
            },
            "cluster": {"tags": {**DEMO_TAGS, "owner": "tester"}, "endpoint": ENDPOINT},
            "repo": {"tags": DEMO_TAGS},
            "access_groups": ["game-agent-monitoring-group"],
            "tagged": ["arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0example"],
            "agones_installed": False,
            "fleet_ports": [4192, 4196],
            "permissions": [OPEN_RULE],
        }
        state.update(overrides)
        self.state.write_text(json.dumps(state))

    def clean_state(self, **overrides) -> None:
        self.write_state(**{"stacks": {}, "cluster": None, "repo": None, "tagged": [], **overrides})

    def read_state(self) -> dict:
        return json.loads(self.state.read_text())

    def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**self.env, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return self.read_state()["calls"]

    def mutating_calls(self) -> list[str]:
        mutating = ("aws cloudformation delete-stack", "aws ecr delete-repository", "eksctl delete",
                    "aws gamelift update-container-fleet")
        return [c for c in self.calls() if c.startswith(mutating) or " helm uninstall" in c or "kubectl delete" in c]

    # -- cluster-up ---------------------------------------------------------

    def test_cluster_up_accepts_only_a_single_ipv4_address(self) -> None:
        for cidr in ("", "0.0.0.0/0", "0.0.0.0/32", "203.0.113.0/24", "203.0.113.0/16", "256.1.1.1/32",
                     "203.0.113.10", "not-a-cidr"):
            result = self.run_script("cluster-up", OPERATOR_CIDR=cidr, OWNER="tester")
            self.assertNotEqual(result.returncode, 0, cidr)
            self.assertFalse(any(c.startswith("eksctl create") for c in self.calls()), cidr)

    def test_cluster_up_renders_every_placeholder_and_the_boundary(self) -> None:
        capture = self.tmp / "rendered.yaml"
        self.write_state(capture=str(capture))
        result = self.run_script("cluster-up", OPERATOR_CIDR="203.0.113.10/32", OWNER="tester")
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = capture.read_text(encoding="utf-8")
        self.assertNotIn("__", rendered)
        self.assertIn("- 203.0.113.10/32", rendered)
        self.assertIn("owner: tester", rendered)
        self.assertRegex(rendered, r"expires-at: \d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z")
        boundary = f"arn:aws:iam::123456789012:policy/{ENV}-workload-boundary"
        self.assertIn(f"serviceRolePermissionsBoundary: {boundary}", rendered)
        self.assertIn(f"instanceRolePermissionsBoundary: {boundary}", rendered)
        self.assertEqual(self.read_state()["created_kubeconfig"], str(self.kubeconfig))

    def test_cluster_up_stops_when_the_boundary_is_missing(self) -> None:
        self.write_state(boundary_exists=False)
        result = self.run_script("cluster-up", OPERATOR_CIDR="203.0.113.10/32", OWNER="tester")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workload-boundary does not exist", result.stderr)
        self.assertFalse(any(c.startswith("eksctl create") for c in self.calls()))

    # -- Kubernetes context -------------------------------------------------

    def test_kubernetes_steps_use_only_the_demo_kubeconfig(self) -> None:
        self.write_state(agones_installed=True)
        result = self.run_script("agones-up", KUBECONFIG=str(self.tmp / "someone-elses-config"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.run_script("down", DEMO_CONFIRM=ENV, KUBECONFIG=str(self.tmp / "someone-elses-config"))
        kube_calls = [c for c in self.calls() if c.startswith(("KUBECONFIG=",))]
        self.assertTrue(any(" helm upgrade" in c for c in kube_calls))
        self.assertTrue(any(" helm uninstall" in c for c in kube_calls))
        for call in kube_calls:
            self.assertTrue(call.startswith(f"KUBECONFIG={self.kubeconfig} "), call)

    def test_kubernetes_steps_stop_when_the_endpoint_does_not_match(self) -> None:
        self.write_state(kube_server="https://OTHER.gr7.us-east-1.eks.amazonaws.com")
        result = self.run_script("agones-up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertFalse(any(" helm " in c for c in self.calls()))

    def test_kubernetes_steps_refuse_an_untagged_cluster(self) -> None:
        self.write_state(cluster={"tags": {}, "endpoint": ENDPOINT})
        for command in (["agones-up"], ["load", "10.0.0.5", "7654"], ["kubeconfig"]):
            result = self.run_script(*command)
            self.assertNotEqual(result.returncode, 0, command)
        self.assertFalse(any(c.startswith("KUBECONFIG=") for c in self.calls()))

    # -- down ---------------------------------------------------------------

    def test_down_deletes_only_this_environment_in_order_and_verifies(self) -> None:
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()

        def first(prefix: str) -> int:
            return next(i for i, c in enumerate(calls) if c.startswith(prefix))

        self.assertLess(first("aws cloudformation delete-stack"), first("eksctl delete cluster"))
        self.assertLess(first("aws cloudformation wait stack-delete-complete"), first("eksctl delete cluster"))
        self.assertLess(first("eksctl delete cluster"), first("aws ecr delete-repository"))
        self.assertLess(first("aws ecr delete-repository"), first("aws resourcegroupstaggingapi get-resources"))
        deleted = sorted(re.search(r"--stack-name (\S+)", c).group(1)
                         for c in calls if c.startswith("aws cloudformation delete-stack"))
        self.assertEqual(deleted, ["gl-demo-migration", "gl-demo-unreal"])
        self.assertIn(f"No resources of environment {ENV} remain", result.stdout)
        tag_call = next(c for c in calls if c.startswith("aws resourcegroupstaggingapi"))
        self.assertIn(f"Key=demo-env,Values={ENV}", tag_call)
        stacks = self.read_state()["stacks"]
        for kept in ("gl-demo-colleague", "gl-demo-untagged", "production-fleet"):
            self.assertEqual(stacks[kept]["status"], "CREATE_COMPLETE", kept)

    def test_down_lists_its_plan_and_requires_confirmation(self) -> None:
        for env in ({}, {"DEMO_CONFIRM": "wrong-name"}):
            self.write_state()
            result = self.run_script("down", **env)
            self.assertNotEqual(result.returncode, 0, env)
            self.assertIn("GameLift stack gl-demo-migration", result.stdout)
            self.assertIn(f"EKS cluster {ENV}", result.stdout)
            self.assertIn("ECR repository", result.stdout)
            self.assertNotIn("gl-demo-colleague", result.stdout)
            self.assertEqual(self.mutating_calls(), [], env)

    def test_down_refuses_a_cluster_without_this_environments_tags(self) -> None:
        self.write_state(cluster={"tags": OTHER_ENV_TAGS, "endpoint": ENDPOINT})
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutating_calls(), [])

    def test_down_keeps_a_repository_without_this_environments_tags(self) -> None:
        self.write_state(repo={"tags": OTHER_ENV_TAGS})
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(c.startswith("aws ecr delete-repository") for c in self.calls()))

    def test_down_waits_for_in_progress_stacks_before_deleting(self) -> None:
        state = self.read_state()
        state["stacks"]["gl-demo-migration"].update(status="CREATE_IN_PROGRESS", settle_after=2)
        self.write_state(stacks=state["stacks"])
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        polls = [i for i, c in enumerate(calls)
                 if "describe-stacks" in c and "gl-demo-migration" in c and "StackStatus" in c]
        delete = next(i for i, c in enumerate(calls)
                      if c.startswith("aws cloudformation delete-stack") and "gl-demo-migration" in c)
        self.assertGreaterEqual(len(polls), 2)
        self.assertLess(polls[1], delete)

    def test_down_fails_when_a_stack_stays_in_progress(self) -> None:
        state = self.read_state()
        state["stacks"]["gl-demo-migration"].update(status="CREATE_IN_PROGRESS", settle_after=1000)
        self.write_state(stacks=state["stacks"])
        result = self.run_script("down", DEMO_CONFIRM=ENV, WAIT_MINUTES="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still CREATE_IN_PROGRESS", result.stderr)
        self.assertFalse(any(c.startswith("eksctl delete") for c in self.calls()))

    def test_down_stops_on_any_aws_error(self) -> None:
        for prefix, message in (
            ("aws cloudformation list-stacks", "(AccessDenied) when calling the ListStacks operation"),
            ("aws cloudformation describe-stacks", "(ExpiredToken) when calling the DescribeStacks operation"),
            ("aws eks describe-cluster", "(AccessDeniedException) when calling the DescribeCluster operation"),
            ("aws ecr describe-repositories", "(ThrottlingException) when calling the DescribeRepositories operation"),
            ("aws ecr list-tags-for-resource", "(AccessDeniedException) when calling ListTagsForResource"),
        ):
            self.write_state(errors={prefix: message})
            result = self.run_script("down", DEMO_CONFIRM=ENV)
            self.assertNotEqual(result.returncode, 0, prefix)
            self.assertEqual(self.mutating_calls(), [], prefix)

    def test_down_never_weakens_protections(self) -> None:
        self.run_script("down", DEMO_CONFIRM=ENV)
        for call in self.calls():
            self.assertNotRegex(
                call, r"termination-protection|deletion-protection|retention|put-bucket-versioning|--retain", call
            )

    def test_down_deletes_a_stack_left_in_review_in_progress_without_waiting(self) -> None:
        state = self.read_state()
        state["stacks"]["gl-demo-migration"].update(status="REVIEW_IN_PROGRESS", settle_after=1000)
        self.write_state(stacks=state["stacks"])
        result = self.run_script("down", DEMO_CONFIRM=ENV, WAIT_MINUTES="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(c.startswith("aws cloudformation delete-stack") and "gl-demo-migration" in c
                            for c in self.calls()))

    def test_review_in_progress_stacks_are_matched_by_their_change_set_tags(self) -> None:
        self.clean_state(stacks={
            "gl-demo-review": {"status": "REVIEW_IN_PROGRESS", "tags": {}, "change_sets": [DEMO_TAGS]},
            "gl-demo-colleague-review": {"status": "REVIEW_IN_PROGRESS", "tags": {}, "change_sets": [OTHER_ENV_TAGS]},
            "gl-demo-mixed-review": {"status": "REVIEW_IN_PROGRESS", "tags": {},
                                     "change_sets": [DEMO_TAGS, OTHER_ENV_TAGS]},
        })
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gl-demo-review REVIEW_IN_PROGRESS", result.stdout)
        self.assertNotIn("colleague", result.stdout)
        self.assertNotIn("mixed", result.stdout)
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deleted = [c for c in self.calls() if c.startswith("aws cloudformation delete-stack")]
        self.assertEqual([re.search(r"--stack-name (\S+)", c).group(1) for c in deleted], ["gl-demo-review"])

    def test_down_removes_eksctl_stacks_left_by_a_failed_cluster_up(self) -> None:
        self.write_state(cluster=None, repo=None, tagged=[], stacks={
            f"eksctl-{ENV}-cluster": {"status": "ROLLBACK_COMPLETE", "tags": DEMO_TAGS},
            f"eksctl-other-env-cluster": {"status": "ROLLBACK_COMPLETE", "tags": OTHER_ENV_TAGS},
        })
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"eksctl stack eksctl-{ENV}-cluster (ROLLBACK_COMPLETE), left by a failed cluster-up", result.stdout)
        self.assertNotIn("other-env", result.stdout)
        self.assertTrue(self.read_state().get("eksctl_deleted"))
        self.assertIn(f"No resources of environment {ENV} remain", result.stdout)

    def test_down_does_not_remove_another_environments_eksctl_stacks(self) -> None:
        self.write_state(cluster=None, repo=None, tagged=[], stacks={
            f"eksctl-{ENV}-cluster": {"status": "ROLLBACK_COMPLETE", "tags": OTHER_ENV_TAGS},
        })
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertFalse(any(c.startswith("eksctl delete") for c in self.calls()))
        self.assertNotEqual(result.returncode, 0)  # verify-clean still reports the name match

    def test_down_deletes_the_default_log_group_of_each_fleet_it_deletes(self) -> None:
        fleet = "containerfleet-00000000-0000-0000-0000-000000000000"
        other = "containerfleet-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        state = self.read_state()
        state["stacks"]["gl-demo-migration"]["fleets"] = [fleet]
        state["stacks"]["gl-demo-colleague"]["fleets"] = [other]
        self.write_state(stacks=state["stacks"],
                         log_groups=[f"gamelift-{fleet}-us-east-1", f"gamelift-{other}-us-east-1"])
        result = self.run_script("down", DEMO_CONFIRM=ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read_state()["log_groups"], [f"gamelift-{other}-us-east-1"])
        # A fleet whose default group never existed is not an error.
        deleted = [c for c in self.calls() if c.startswith("aws logs delete-log-group")]
        self.assertEqual(len(deleted), 1)

    # -- verify-clean -------------------------------------------------------

    def test_verify_clean_reports_remaining_fleet_log_groups(self) -> None:
        self.clean_state(log_groups=["gamelift-gl-demo-reference", "gamelift-other-reference"])
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gamelift-gl-demo-reference", result.stdout)
        self.assertNotIn("other-reference", result.stdout)

    def test_verify_clean_fails_when_anything_remains(self) -> None:
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("natgateway/nat-0example", result.stdout)
        self.assertIn("gl-demo-migration", result.stdout)
        self.assertNotIn("gl-demo-colleague", result.stdout)
        self.assertIn("Cluster remains", result.stdout)
        self.assertIn("ECR repository remains", result.stdout)

    def test_verify_clean_reports_stacks_in_every_status_but_deleted(self) -> None:
        for status in ("CREATE_IN_PROGRESS", "CREATE_FAILED", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE",
                       "UPDATE_ROLLBACK_FAILED", "UPDATE_ROLLBACK_COMPLETE", "DELETE_FAILED",
                       "DELETE_IN_PROGRESS", "IMPORT_ROLLBACK_COMPLETE"):
            self.clean_state(stacks={
                "gl-demo-x": {"status": status, "tags": DEMO_TAGS},
                f"eksctl-{ENV}-nodegroup-gameservers": {"status": status, "tags": DEMO_TAGS},
            })
            result = self.run_script("verify-clean")
            self.assertNotEqual(result.returncode, 0, status)
            self.assertIn(f"gl-demo-x {status}", result.stdout)
            self.assertIn(f"eksctl-{ENV}-nodegroup-gameservers", result.stdout)

    def test_verify_clean_fails_on_any_aws_error(self) -> None:
        for prefix in (
            "aws resourcegroupstaggingapi get-resources",
            "aws cloudformation list-stacks",
            "aws eks describe-cluster",
            "aws ecr describe-repositories",
        ):
            self.clean_state(errors={prefix: "(AccessDeniedException) when calling the operation"})
            result = self.run_script("verify-clean")
            self.assertNotEqual(result.returncode, 0, prefix)
            self.assertNotIn("remain in", result.stdout, prefix)

    def test_verify_clean_fails_on_denied_tagging_call_while_a_nat_gateway_exists(self) -> None:
        self.clean_state(
            tagged=["arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0example"],
            errors={"aws resourcegroupstaggingapi": "(AccessDeniedException) when calling GetResources"},
        )
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("No resources", result.stdout)

    def test_verify_clean_ignores_stale_tag_listings_for_gone_ec2_resources(self) -> None:
        stale = [
            "arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0gone",
            "arn:aws:ec2:us-east-1:123456789012:instance/i-0gone",
            "arn:aws:ec2:us-east-1:123456789012:volume/vol-0gone",
        ]
        self.clean_state()
        state = self.read_state()
        state.update(tagged=stale, nat_state="notfound", instance_state="terminated", volume_state="None")
        self.state.write_text(json.dumps(state))
        result = self.run_script("verify-clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verify_clean_reports_ec2_resources_that_still_exist(self) -> None:
        self.clean_state()
        state = self.read_state()
        state.update(tagged=["arn:aws:ec2:us-east-1:123456789012:volume/vol-0still"], volume_state="in-use")
        self.state.write_text(json.dumps(state))
        result = self.run_script("verify-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vol-0still", result.stdout)

    def test_verify_clean_retries_before_reporting(self) -> None:
        self.run_script("verify-clean")
        tag_calls = [c for c in self.calls() if c.startswith("aws resourcegroupstaggingapi")]
        self.assertEqual(len(tag_calls), 2)

    def test_verify_clean_does_not_count_other_environments(self) -> None:
        self.clean_state(
            stacks={"gl-demo-colleague": {"status": "CREATE_COMPLETE", "tags": OTHER_ENV_TAGS}},
            cluster={"tags": OTHER_ENV_TAGS, "endpoint": ENDPOINT},
            repo={"tags": OTHER_ENV_TAGS},
        )
        result = self.run_script("verify-clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verify_clean_passes_when_empty(self) -> None:
        self.clean_state()
        result = self.run_script("verify-clean")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # -- restrict-fleet-ingress ---------------------------------------------

    def _update_call(self) -> str:
        return next(c for c in self.calls() if c.startswith("aws gamelift update-container-fleet"))

    def test_restrict_fleet_ingress_replaces_every_other_rule_with_the_nat_address(self) -> None:
        tcp = {"FromPort": 4192, "ToPort": 4196, "IpRange": "203.0.113.0/24", "Protocol": "TCP"}
        ipv6 = {"FromPort": 1025, "ToPort": 60000, "IpRange": "::/0", "Protocol": "UDP"}
        self.write_state(permissions=[OPEN_RULE, tcp, ipv6])
        result = self.run_script("restrict-fleet-ingress", "gl-demo-migration")
        self.assertEqual(result.returncode, 0, result.stderr)
        update = self._update_call()
        auth = re.search(r"--instance-inbound-permission-authorizations (\[.*?\])", update).group(1)
        revoke = re.search(r"--instance-inbound-permission-revocations (\[.*?\])", update).group(1)
        self.assertEqual(json.loads(auth), [NAT_RULE])
        self.assertCountEqual(json.loads(revoke), [OPEN_RULE, tcp, ipv6])
        self.assertEqual(self.read_state()["permissions"], [NAT_RULE])

    def test_restrict_fleet_ingress_fails_when_the_result_is_not_exact(self) -> None:
        self.write_state(update_ignored=True)
        result = self.run_script("restrict-fleet-ingress", "gl-demo-migration")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("are not exactly", result.stderr)

    def test_restrict_fleet_ingress_leaves_an_already_restricted_fleet_alone(self) -> None:
        self.write_state(permissions=[NAT_RULE])
        result = self.run_script("restrict-fleet-ingress", "gl-demo-migration")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("aws gamelift update-container-fleet") for c in self.calls()))

    def test_restrict_fleet_ingress_rejects_non_integer_ports(self) -> None:
        for ports in (["4192", "abc"], ["0", "4196"], ["4196", "4192"], ["4192", "70000"]):
            self.write_state(fleet_ports=ports)
            result = self.run_script("restrict-fleet-ingress", "gl-demo-migration")
            self.assertNotEqual(result.returncode, 0, ports)
            self.assertEqual(self.mutating_calls(), [], ports)

    def test_restrict_fleet_ingress_only_touches_this_environments_stacks(self) -> None:
        for stack in ("production-fleet", "gl-demo-colleague", "gl-demo-untagged", "gl-demo-missing"):
            self.write_state()
            result = self.run_script("restrict-fleet-ingress", stack)
            self.assertNotEqual(result.returncode, 0, stack)
            self.assertEqual(self.mutating_calls(), [], stack)

    # -- argument validation ------------------------------------------------

    def test_load_rejects_bad_targets_and_bounds(self) -> None:
        for args in (["$(touch /tmp/x)", "7654"], ["10.0.0.5", "70000"], ["10.0.0.5", "0"],
                     ["10.0.0.5", "7654", "0"], ["10.0.0.5", "7654", "8", "99999"], ["999.0.0.1", "7654"]):
            result = self.run_script("load", *args)
            self.assertNotEqual(result.returncode, 0, args)
            self.assertFalse(any("kubectl apply" in c for c in self.calls()), args)

    def test_load_sets_the_job_deadline_from_the_run_time(self) -> None:
        result = self.run_script("load", "10.0.0.5", "7654", "8", "30")
        self.assertEqual(result.returncode, 0, result.stderr)
        (manifest,) = self.read_state()["applied"]
        self.assertIn("activeDeadlineSeconds: 330", manifest)
        self.assertIn('value: "10.0.0.5"', manifest)

    def test_helper_commands_print_values_for_the_deploy_commands(self) -> None:
        result = self.run_script("nat-cidr")
        self.assertEqual(result.stdout.strip(), "198.51.100.7/32", result.stderr)
        result = self.run_script("boundary-arn")
        self.assertEqual(result.stdout.strip(), f"arn:aws:iam::123456789012:policy/{ENV}-workload-boundary")
        result = self.run_script("kubeconfig")
        self.assertEqual(result.stdout.strip(), str(self.kubeconfig), result.stderr)


if __name__ == "__main__":
    unittest.main()
