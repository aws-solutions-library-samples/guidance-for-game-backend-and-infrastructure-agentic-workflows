"""Behavior tests for EKS enrollment and deregistration scripts.

All AWS, eksctl, and kubectl calls are local fakes. These tests never use
credentials, kubeconfig, or live infrastructure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBERNETES_DIR = REPOSITORY_ROOT / "infrastructure" / "kubernetes"
ACCOUNT_ID = "123456789012"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/game-agent-agentcore-execution-role"
MONITORING_GROUP = "game-agent-monitoring-group"
MONITORING_ROLE = "game-agent-monitoring-role"
MONITORING_BINDING = "game-agent-monitoring-binding"
MANAGED_LABELS = {
    "app.kubernetes.io/name": "game-agent",
    "app.kubernetes.io/component": "rbac",
    "app.kubernetes.io/managed-by": "game-agent-enrollment",
}

AWS_FAKE = r'''
import copy
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("aws_calls", []).append(" ".join(args))
state.setdefault("events", []).append("aws " + " ".join(args))


def save():
    state_path.write_text(json.dumps(state))


def option(name, default=None):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return default


def groups_from_option():
    value = option("--kubernetes-groups", "[]")
    if value.startswith("["):
        return json.loads(value)
    return [value]


command = tuple(args[:2])
if command == ("sts", "get-caller-identity"):
    save()
    if option("--query") == "Account":
        print("123456789012")
    else:
        print("arn:aws:sts::123456789012:assumed-role/TestRole/test-session")
elif command == ("eks", "describe-cluster"):
    save()
    print(state.get("authentication_mode", "API"))
elif command == ("eks", "update-kubeconfig"):
    save()
    sys.exit(state.get("update_kubeconfig_rc", 0))
elif command == ("eks", "describe-access-entry"):
    error = state.get("describe_access_error")
    entry = state.get("access_entry")
    if error:
        save()
        print(f"An error occurred ({error}) when calling DescribeAccessEntry", file=sys.stderr)
        sys.exit(254)
    if entry is None:
        save()
        print("An error occurred (ResourceNotFoundException) when calling DescribeAccessEntry", file=sys.stderr)
        sys.exit(254)

    visible_entry = copy.deepcopy(entry)
    remaining = state.get("visibility_remaining", 0)
    if remaining > 0:
        state["visibility_remaining"] = remaining - 1
        if state.get("last_access_operation") == "create":
            save()
            print("An error occurred (ResourceNotFoundException) when calling DescribeAccessEntry", file=sys.stderr)
            sys.exit(254)
        visible_entry["kubernetesGroups"] = state.get("previous_visible_groups", [])
    save()
    print(json.dumps({"accessEntry": visible_entry}))
elif command == ("eks", "list-associated-access-policies"):
    error = state.get("list_policies_error")
    save()
    if error:
        print(f"An error occurred ({error}) when calling ListAssociatedAccessPolicies", file=sys.stderr)
        sys.exit(254)
    print(json.dumps({"associatedAccessPolicies": state.get("policies", [])}))
elif command == ("eks", "create-access-entry"):
    if state.get("create_access_rc", 0):
        save()
        sys.exit(state["create_access_rc"])
    tags = {}
    tag_value = option("--tags")
    if tag_value and "=" in tag_value:
        key, value = tag_value.split("=", 1)
        tags[key] = value
    state["access_entry"] = {
        "clusterName": option("--cluster-name"),
        "principalArn": option("--principal-arn"),
        "kubernetesGroups": groups_from_option(),
        "username": option("--username"),
        "type": option("--type", "STANDARD"),
        "tags": tags,
    }
    state["last_access_operation"] = "create"
    state["visibility_remaining"] = state.get("post_mutation_visibility_delay", 0)
    save()
elif command == ("eks", "update-access-entry"):
    if state.get("update_access_rc", 0):
        save()
        sys.exit(state["update_access_rc"])
    state["previous_visible_groups"] = state["access_entry"].get("kubernetesGroups", [])
    state["access_entry"]["kubernetesGroups"] = groups_from_option()
    state["last_access_operation"] = "update"
    state["visibility_remaining"] = state.get("post_mutation_visibility_delay", 0)
    save()
elif command == ("eks", "delete-access-entry"):
    if state.get("delete_access_rc", 0):
        save()
        sys.exit(state["delete_access_rc"])
    state["access_entry"] = None
    state["last_access_operation"] = "delete"
    save()
elif command == ("eks", "update-cluster-config"):
    save()
    print("fake-update-id")
elif command == ("logs", "put-retention-policy"):
    save()
else:
    save()
'''

KUBECTL_FAKE = r'''
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("kubectl_calls", []).append(" ".join(args))
state.setdefault("events", []).append("kubectl " + " ".join(args))


def save():
    state_path.write_text(json.dumps(state))


def option_prefix(prefix, default=None):
    for value in args:
        if value.startswith(prefix):
            return value.split("=", 1)[1]
    return default


def binding(name, role, group):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name, "labels": {}},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": group}],
    }


if args[:1] == ["cluster-info"]:
    save()
    sys.exit(0 if state.get("cluster_info", True) else 1)

if args[:2] == ["create", "clusterrolebinding"]:
    name = args[2]
    document = binding(name, option_prefix("--clusterrole="), option_prefix("--group="))
    save()
    print(json.dumps(document))
    sys.exit(0)

if args[:1] == ["apply"]:
    if "--dry-run=server" in args:
        save()
        sys.exit(0 if state.get("dry_run_allowed", True) else 1)
    if "-f" in args and args[args.index("-f") + 1] == "-":
        document = json.loads(sys.stdin.read())
        if state.get("binding_apply_rc", 0):
            if state.get("binding_apply_partial", False):
                state.setdefault("bindings", {})[document["metadata"]["name"]] = document
            save()
            sys.exit(state["binding_apply_rc"])
        state.setdefault("bindings", {})[document["metadata"]["name"]] = document
    else:
        if state.get("role_apply_rc", 0):
            if state.get("role_apply_partial", False):
                state["role_exists"] = True
            save()
            sys.exit(state["role_apply_rc"])
        state["role_exists"] = True
        state["role_labels"] = {
            "app.kubernetes.io/name": "game-agent",
            "app.kubernetes.io/component": "rbac",
            "app.kubernetes.io/managed-by": "game-agent-enrollment",
        }
    save()
    sys.exit(0)

if args[:2] == ["auth", "can-i"]:
    verb = args[2] if len(args) > 2 else ""
    resource = args[3] if len(args) > 3 else ""
    group = option_prefix("--as-group=")
    has_user = any(value.startswith("--as=") for value in args)
    permission_key = f"{verb} {resource}"

    if state.get("auth_error_for") == permission_key:
        save()
        print(f"simulated authorization API error for {permission_key}", file=sys.stderr)
        sys.exit(2)

    if not has_user and verb == "delete" and resource.startswith("clusterrole"):
        answer = "yes" if state.get("cleanup_allowed", True) else "no"
    elif permission_key in state.get("dangerous_permissions", []):
        answer = "yes"
    elif verb == "list" and resource in {"pods", "nodes"}:
        has_binding = any(
            item.get("roleRef", {}).get("name") == "game-agent-monitoring-role"
            and any(subject.get("kind") == "Group" and subject.get("name") == group for subject in item.get("subjects", []))
            for item in state.get("bindings", {}).values()
        )
        answer = "yes" if state.get("role_exists", False) and has_binding else "no"
    else:
        answer = "no"

    save()
    print(answer)
    sys.exit(0 if answer == "yes" else 1)

if args[:2] == ["get", "clusterrole"]:
    error = state.get("role_get_error")
    if error:
        save()
        print(f"Error from server ({error}): unable to read clusterrole", file=sys.stderr)
        sys.exit(1)
    if not state.get("role_exists", False):
        save()
        print(f'Error from server (NotFound): clusterroles "{args[2]}" not found', file=sys.stderr)
        sys.exit(1)
    save()
    if "-o" in args and args[args.index("-o") + 1] == "json":
        print(json.dumps({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": args[2], "labels": state.get("role_labels", {})},
        }))
    sys.exit(0)

if args[:2] == ["get", "clusterrolebinding"]:
    error = state.get("binding_get_error")
    item = state.get("bindings", {}).get(args[2])
    save()
    if error:
        print(f"Error from server ({error}): unable to read clusterrolebinding", file=sys.stderr)
        sys.exit(1)
    if item is None:
        print(f'Error from server (NotFound): clusterrolebindings "{args[2]}" not found', file=sys.stderr)
        sys.exit(1)
    if "-o" in args and args[args.index("-o") + 1] == "json":
        print(json.dumps(item))
    sys.exit(0)

if args[:2] == ["get", "clusterrolebindings"]:
    save()
    print(json.dumps({"items": list(state.get("bindings", {}).values())}))
    sys.exit(0)

if args[:2] == ["get", "rolebindings,clusterrolebindings"]:
    items = list(state.get("bindings", {}).values()) + state.get("extra_bindings", [])
    save()
    print(json.dumps({"items": items}))
    sys.exit(0)

if args[:3] == ["get", "configmap", "aws-auth"]:
    error = state.get("config_map_error")
    state["config_map_reads"] = state.get("config_map_reads", 0) + 1
    if state.get("config_map_after_prompt") is not None and state["config_map_reads"] >= 2:
        state["config_map_entries"] = state["config_map_after_prompt"]
    save()
    if error:
        print(f"Error from server ({error}): unable to read configmap aws-auth", file=sys.stderr)
        sys.exit(1)
    if not state.get("config_map_exists", True):
        print('Error from server (NotFound): configmaps "aws-auth" not found', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "aws-auth", "namespace": "kube-system"},
        "data": {"mapRoles": json.dumps(state.get("config_map_entries", []))},
    }))
    sys.exit(0)

if args[:2] == ["delete", "clusterrolebinding"]:
    state.setdefault("bindings", {}).pop(args[2], None)
    save()
    sys.exit(state.get("delete_binding_rc", 0))

if args[:2] == ["delete", "clusterrole"]:
    if not state.get("delete_role_rc", 0):
        state["role_exists"] = False
    save()
    sys.exit(state.get("delete_role_rc", 0))

save()
'''

EKSCTL_FAKE = r'''
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("eksctl_calls", []).append(" ".join(args))
state.setdefault("events", []).append("eksctl " + " ".join(args))


def option(name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return ""


role_arn = option("--arn")
if args[:2] == ["create", "iamidentitymapping"]:
    state.setdefault("config_map_entries", []).append(
        {
            "rolearn": role_arn,
            "username": option("--username"),
            "groups": [option("--group")],
        }
    )
elif args[:2] == ["delete", "iamidentitymapping"]:
    state["config_map_entries"] = [
        entry for entry in state.get("config_map_entries", []) if entry.get("rolearn") != role_arn
    ]
state_path.write_text(json.dumps(state))
'''

YQ_FAKE = r'''
import json
import sys

args = sys.argv[1:]
content = sys.stdin.read()
if "-r" in args:
    document = json.loads(content)
    print(document.get("data", {}).get("mapRoles", ""))
elif "-p=yaml" in args:
    print(json.dumps(json.loads(content)))
else:
    sys.exit(1)
'''


def binding_document(name: str, group: str, *, role: str = MONITORING_ROLE) -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name, "labels": MANAGED_LABELS.copy()},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": group}],
    }


def role_binding_document(
    name: str,
    *,
    group: str | None = None,
    user: str | None = None,
    role: str = "example",
    role_ref_kind: str = "Role",
) -> dict:
    subjects = []
    if group is not None:
        subjects.append({"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": group})
    if user is not None:
        subjects.append({"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": user})
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": "example"},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": role_ref_kind, "name": role},
        "subjects": subjects,
    }


def config_map_entry(
    role_arn: str = ROLE_ARN,
    *,
    username: str = "game-agent-agentcore-user",
    group: str = MONITORING_GROUP,
) -> dict:
    return {"rolearn": role_arn, "username": username, "groups": [group]}


class ScriptFixture:
    def __init__(self, *, include_jq: bool = True, include_eksctl: bool = False):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="eks-access-tests-")
        self.root = Path(self.temporary_directory.name)
        self.script_dir = self.root / "infrastructure" / "kubernetes"
        self.fake_bin = self.root / "fake-bin"
        self.tool_bin = self.root / "tool-bin"
        self.script_dir.mkdir(parents=True)
        self.fake_bin.mkdir()
        self.tool_bin.mkdir()

        for filename in (
            "enroll-cluster.sh",
            "deregister-cluster.sh",
            "game-agent-monitoring-rbac.yaml",
        ):
            shutil.copy2(KUBERNETES_DIR / filename, self.script_dir / filename)

        self._write_executable("aws", AWS_FAKE)
        self._write_executable("kubectl", KUBECTL_FAKE)
        self._write_executable("yq", YQ_FAKE)
        if include_eksctl:
            self._write_executable("eksctl", EKSCTL_FAKE)

        for command in ("date", "dirname", "grep", "rm", "sleep"):
            self._link_tool(command)
        hash_tool = shutil.which("sha256sum") or shutil.which("shasum")
        if hash_tool is None:
            raise RuntimeError("sha256sum or shasum is required to run EKS access script tests")
        (self.tool_bin / Path(hash_tool).name).symlink_to(hash_tool)

        if include_jq:
            self._link_tool("jq")

        self.state_path = self.root / "state.json"
        self.write_state(
            {
                "authentication_mode": "API",
                "access_entry": None,
                "policies": [],
                "cluster_info": True,
                "dry_run_allowed": True,
                "cleanup_allowed": True,
                "role_exists": False,
                "role_labels": MANAGED_LABELS.copy(),
                "bindings": {},
                "extra_bindings": [],
                "dangerous_permissions": [],
                "config_map_exists": True,
                "config_map_entries": [],
                "aws_calls": [],
                "kubectl_calls": [],
                "eksctl_calls": [],
                "events": [],
            }
        )

    def _write_executable(self, name: str, body: str) -> None:
        path = self.fake_bin / name
        path.write_text(f"#!{sys.executable}\n{textwrap.dedent(body).lstrip()}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _link_tool(self, command: str) -> None:
        source = shutil.which(command)
        if source is None:
            raise RuntimeError(f"Required test utility not found: {command}")
        (self.tool_bin / command).symlink_to(source)

    def write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state))

    def read_state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def update_state(self, **updates) -> None:
        state = self.read_state()
        state.update(updates)
        self.write_state(state)

    def run(self, script: str, *, input_text: str = "", extra_args: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["FAKE_STATE"] = str(self.state_path)
        env["PATH"] = os.pathsep.join((str(self.fake_bin), str(self.tool_bin)))
        env["EKS_ACCESS_ENTRY_POLL_ATTEMPTS"] = "4"
        env["EKS_ACCESS_ENTRY_POLL_DELAY_SECONDS"] = "0"
        return subprocess.run(
            ["/bin/bash", str(self.script_dir / script), "demo-cluster", "us-west-2", *extra_args],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=20,
            check=False,
        )

    def close(self) -> None:
        self.temporary_directory.cleanup()


class EksClusterAccessScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ScriptFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def managed_entry(self, *, groups: list[str] | None = None, username: str = "game-agent-agentcore-user", managed: bool = True) -> dict:
        return {
            "principalArn": ROLE_ARN,
            "kubernetesGroups": groups if groups is not None else [MONITORING_GROUP],
            "username": username,
            "tags": {"GameAgentManaged": "true"} if managed else {},
        }

    def configure_existing_default_rbac(self, *, include_other_binding: bool = False) -> None:
        bindings = {MONITORING_BINDING: binding_document(MONITORING_BINDING, MONITORING_GROUP)}
        if include_other_binding:
            bindings["game-agent-monitoring-other"] = binding_document(
                "game-agent-monitoring-other", "game-agent-monitoring-other"
            )
        self.fixture.update_state(role_exists=True, bindings=bindings)

    def test_api_enrollment_creates_group_access_entry_without_policy(self) -> None:
        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(state["access_entry"]["principalArn"], ROLE_ARN)
        self.assertEqual(state["access_entry"]["kubernetesGroups"], [MONITORING_GROUP])
        self.assertEqual(state["access_entry"]["tags"], {"GameAgentManaged": "true"})
        self.assertIn(MONITORING_BINDING, state["bindings"])
        self.assertFalse(any("associate-access-policy" in call for call in state["aws_calls"]))
        binding_apply_index = state["events"].index("kubectl apply -f -")
        access_entry_create_index = next(
            index
            for index, event in enumerate(state["events"])
            if event.startswith("aws eks create-access-entry")
        )
        self.assertLess(binding_apply_index, access_entry_create_index)
        self.assertTrue(
            any(call.startswith("auth can-i list pods --all-namespaces") for call in state["kubectl_calls"])
        )
        self.assertTrue(
            any(call.startswith("auth can-i list nodes --all-namespaces") for call in state["kubectl_calls"])
        )
        self.assertIn("Enrollment complete", result.stdout)

    def test_api_enrollment_is_idempotent(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(access_entry=self.managed_entry())

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertFalse(any("update-access-entry" in call for call in state["aws_calls"]))
        self.assertEqual(state["access_entry"]["kubernetesGroups"], [MONITORING_GROUP])

    def test_delayed_access_entry_visibility_is_retried(self) -> None:
        self.fixture.update_state(post_mutation_visibility_delay=2)

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        describe_calls = [call for call in state["aws_calls"] if call.startswith("eks describe-access-entry")]
        self.assertGreaterEqual(len(describe_calls), 4)
        self.assertIn("access-entry configuration observed", result.stdout)

    def test_failed_binding_apply_rolls_back_entry_and_new_role(self) -> None:
        self.fixture.update_state(binding_apply_rc=1, binding_apply_partial=True)

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(state["access_entry"])
        self.assertFalse(state["role_exists"])
        self.assertNotIn(MONITORING_BINDING, state["bindings"])
        self.assertIn("rolled back", result.stdout)

    def test_permission_probe_error_fails_closed_and_rolls_back(self) -> None:
        self.fixture.update_state(auth_error_for="delete pods")

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(state["access_entry"])
        self.assertFalse(state["role_exists"])
        self.assertEqual(state["bindings"], {})
        self.assertIn("Failed to verify pod delete denial", result.stdout)

    def test_custom_role_uses_principal_specific_group_and_binding(self) -> None:
        result = self.fixture.run("enroll-cluster.sh", extra_args=("--role-name", "custom-role"))
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        groups = state["access_entry"]["kubernetesGroups"]
        self.assertEqual(len(groups), 1)
        self.assertNotEqual(groups[0], MONITORING_GROUP)
        self.assertNotIn(MONITORING_BINDING, state["bindings"])
        self.assertEqual(len(state["bindings"]), 1)
        generated_binding = next(iter(state["bindings"].values()))
        self.assertEqual(
            generated_binding["metadata"]["labels"]["app.kubernetes.io/managed-by"],
            "game-agent-enrollment",
        )

        second_enrollment = self.fixture.run(
            "enroll-cluster.sh", extra_args=("--role-name", "custom-role")
        )
        self.assertEqual(second_enrollment.returncode, 0, second_enrollment.stdout)

        deregistration = self.fixture.run(
            "deregister-cluster.sh",
            input_text="y\n",
            extra_args=("--role-name", "custom-role"),
        )
        self.assertEqual(deregistration.returncode, 0, deregistration.stdout)

    def test_kubernetes_administrator_role_is_forwarded_to_kubeconfig(self) -> None:
        admin_role = f"arn:aws:iam::{ACCOUNT_ID}:role/eks-admin"

        result = self.fixture.run(
            "enroll-cluster.sh",
            extra_args=("--kube-role-arn", admin_role),
        )
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        update_calls = [call for call in state["aws_calls"] if call.startswith("eks update-kubeconfig")]
        self.assertEqual(len(update_calls), 1)
        self.assertIn(f"--role-arn {admin_role}", update_calls[0])

    def test_monitoring_manifest_contains_only_shared_cluster_role(self) -> None:
        manifest = (KUBERNETES_DIR / "game-agent-monitoring-rbac.yaml").read_text()

        self.assertIn("kind: ClusterRole\n", manifest)
        self.assertNotIn("kind: ClusterRoleBinding\n", manifest)
        self.assertNotIn("kind: Namespace\n", manifest)
        self.assertNotIn("kind: NetworkPolicy\n", manifest)

    def test_missing_jq_fails_before_any_aws_or_kubernetes_call(self) -> None:
        self.fixture.close()
        self.fixture = ScriptFixture(include_jq=False)

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["aws_calls"], [])
        self.assertEqual(state["kubectl_calls"], [])
        self.assertIn("jq not found", result.stdout)

    def test_existing_policy_is_rejected_before_authentication_mutation(self) -> None:
        self.fixture.update_state(
            access_entry=self.managed_entry(groups=[], username="existing-user", managed=False),
            policies=[
                {
                    "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy",
                    "accessScope": {"type": "namespace", "namespaces": ["example"]},
                }
            ],
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertFalse(any("update-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("policy-based permissions", result.stdout)

    def test_access_entry_read_error_does_not_trigger_creation(self) -> None:
        self.fixture.update_state(describe_access_error="ThrottlingException")

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("Failed to read the access entry", result.stdout)

    def test_session_templated_access_entry_username_is_rejected(self) -> None:
        templated_username = (
            f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/example/" + "{{SessionName}}"
        )
        self.fixture.update_state(
            access_entry=self.managed_entry(username=templated_username, managed=False)
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("update-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("session-templated username", result.stdout)

    def test_non_administrator_fails_before_access_entry_mutation(self) -> None:
        self.fixture.update_state(dry_run_allowed=False)

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("describe-access-entry" in call for call in state["aws_calls"]))
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("cannot apply", result.stdout)

    def test_rbac_read_error_stops_enrollment_before_mutation(self) -> None:
        self.fixture.update_state(role_get_error="Forbidden")

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("Failed to inspect the existing monitoring ClusterRole", result.stdout)

    def test_rbac_read_error_stops_deregistration_before_mutation(self) -> None:
        self.fixture.update_state(
            role_get_error="Forbidden",
            access_entry=self.managed_entry(),
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("delete-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("Failed to inspect the monitoring ClusterRole", result.stdout)

    def test_unrelated_binding_to_monitoring_group_is_rejected(self) -> None:
        self.fixture.update_state(
            extra_bindings=[role_binding_document("unexpected", group=MONITORING_GROUP)]
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("unrelated RBAC bindings", result.stdout)

    def test_unrelated_binding_to_access_entry_username_is_rejected(self) -> None:
        self.fixture.update_state(
            extra_bindings=[role_binding_document("unexpected-user", user="game-agent-agentcore-user")]
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("unrelated RBAC bindings", result.stdout)

    def test_custom_role_binding_requires_ownership_label(self) -> None:
        role_name = "custom-role"
        role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
        principal_id = hashlib.sha256(role_arn.encode()).hexdigest()[:16]
        group = f"game-agent-monitoring-{principal_id}"
        binding_name = f"game-agent-monitoring-{principal_id}"
        unowned_binding = binding_document(binding_name, group)
        unowned_binding["metadata"]["labels"] = {}
        self.fixture.update_state(bindings={binding_name: unowned_binding})

        result = self.fixture.run("enroll-cluster.sh", extra_args=("--role-name", role_name))
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("ownership label", result.stdout)

    def test_config_map_read_error_stops_dual_mode_enrollment(self) -> None:
        self.fixture.update_state(
            authentication_mode="API_AND_CONFIG_MAP",
            config_map_error="Forbidden",
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("Failed to read aws-auth", result.stdout)

    def test_config_map_existing_privileged_tuple_is_rejected(self) -> None:
        self.fixture.update_state(
            authentication_mode="CONFIG_MAP",
            config_map_entries=[
                config_map_entry(username="unexpected-admin", group="system:masters")
            ],
        )

        result = self.fixture.run("enroll-cluster.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exactly match", result.stdout)

    def test_legacy_custom_config_mapping_directs_user_to_deregister(self) -> None:
        role_name = "legacy-custom-role"
        role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
        self.fixture.update_state(
            authentication_mode="CONFIG_MAP",
            config_map_entries=[config_map_entry(role_arn)],
        )

        result = self.fixture.run(
            "enroll-cluster.sh", extra_args=("--role-name", role_name)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy custom-role aws-auth mapping", result.stdout)
        self.assertIn("Deregister this role", result.stdout)

    def test_api_and_config_map_enrollment_rejects_duplicate_legacy_mapping(self) -> None:
        self.fixture.update_state(
            authentication_mode="API_AND_CONFIG_MAP",
            config_map_entries=[config_map_entry()],
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))
        self.assertIn("also contains an aws-auth mapping", result.stdout)

    def test_aws_auth_role_match_is_exact_not_prefix_based(self) -> None:
        self.fixture.update_state(
            authentication_mode="API_AND_CONFIG_MAP",
            config_map_entries=[config_map_entry(f"{ROLE_ARN}-backup")],
        )

        result = self.fixture.run("enroll-cluster.sh")

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_api_deregistration_removes_mapping_and_preserves_legacy_shared_rbac(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(access_entry=self.managed_entry())

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(state["access_entry"])
        self.assertTrue(state["role_exists"])
        self.assertIn(MONITORING_BINDING, state["bindings"])
        self.assertNotIn("delete -f", "\n".join(state["kubectl_calls"]))
        self.assertIn("Legacy shared ClusterRoleBinding: Preserved", result.stdout)
        self.assertIn("Namespaces and workloads: Preserved", result.stdout)

    def test_deregistration_preserves_shared_role_for_other_principal(self) -> None:
        self.configure_existing_default_rbac(include_other_binding=True)
        self.fixture.update_state(access_entry=self.managed_entry())

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(MONITORING_BINDING, state["bindings"])
        self.assertIn("game-agent-monitoring-other", state["bindings"])
        self.assertTrue(state["role_exists"])
        self.assertIn("preserved for 2 remaining binding", result.stdout)

    def test_deregistration_preserves_role_used_by_namespaced_binding(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            access_entry=self.managed_entry(),
            extra_bindings=[
                role_binding_document(
                    "namespaced-consumer",
                    group="unrelated-group",
                    role=MONITORING_ROLE,
                    role_ref_kind="ClusterRole",
                )
            ],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(state["role_exists"])
        self.assertIn("preserved for 2 remaining binding", result.stdout)

    def test_default_deregistration_preserves_legacy_custom_role_access(self) -> None:
        self.fixture.close()
        self.fixture = ScriptFixture(include_eksctl=True)
        self.configure_existing_default_rbac()
        custom_role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/legacy-custom-role"
        self.fixture.update_state(
            authentication_mode="CONFIG_MAP",
            config_map_entries=[
                config_map_entry(),
                config_map_entry(custom_role_arn),
            ],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(any(entry["rolearn"] == custom_role_arn for entry in state["config_map_entries"]))
        self.assertIn(MONITORING_BINDING, state["bindings"])
        self.assertTrue(state["role_exists"])

    def test_legacy_custom_config_mapping_can_be_deregistered(self) -> None:
        self.fixture.close()
        self.fixture = ScriptFixture(include_eksctl=True)
        self.configure_existing_default_rbac()
        role_name = "legacy-custom-role"
        role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
        self.fixture.update_state(
            authentication_mode="CONFIG_MAP",
            config_map_entries=[config_map_entry(role_arn)],
        )

        result = self.fixture.run(
            "deregister-cluster.sh",
            input_text="y\n",
            extra_args=("--role-name", role_name),
        )
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(any(entry["rolearn"] == role_arn for entry in state["config_map_entries"]))
        self.assertIn(MONITORING_BINDING, state["bindings"])
        self.assertTrue(state["role_exists"])

    def test_legacy_custom_api_access_entry_can_be_deregistered(self) -> None:
        self.configure_existing_default_rbac()
        role_name = "legacy-custom-role"
        role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
        self.fixture.update_state(
            access_entry={
                "principalArn": role_arn,
                "kubernetesGroups": [MONITORING_GROUP],
                "username": "game-agent-agentcore-user",
                "tags": {"GameAgentManaged": "true"},
            }
        )

        result = self.fixture.run(
            "deregister-cluster.sh",
            input_text="y\n",
            extra_args=("--role-name", role_name),
        )
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(state["access_entry"])
        self.assertIn(MONITORING_BINDING, state["bindings"])
        self.assertTrue(state["role_exists"])

    def test_deregistration_refuses_mismatched_config_map_tuple(self) -> None:
        self.fixture.close()
        self.fixture = ScriptFixture(include_eksctl=True)
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            authentication_mode="CONFIG_MAP",
            config_map_entries=[
                config_map_entry(username="unexpected-admin", group="system:masters")
            ],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(state["config_map_entries"]), 1)
        self.assertFalse(any(call.startswith("delete iamidentitymapping") for call in state["eksctl_calls"]))
        self.assertIn("refusing to delete", result.stdout)

    def test_dual_mode_deregistration_removes_config_map_before_access_entry(self) -> None:
        self.fixture.close()
        self.fixture = ScriptFixture(include_eksctl=True)
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            authentication_mode="API_AND_CONFIG_MAP",
            access_entry=self.managed_entry(),
            config_map_entries=[config_map_entry()],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(any(entry["rolearn"] == ROLE_ARN for entry in state["config_map_entries"]))
        self.assertIsNone(state["access_entry"])
        delete_mapping_index = next(
            index for index, call in enumerate(state["eksctl_calls"]) if call.startswith("delete iamidentitymapping")
        )
        self.assertEqual(delete_mapping_index, 0)

    def test_deregistration_preserves_unrelated_groups_and_reports_incomplete(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            access_entry=self.managed_entry(groups=[MONITORING_GROUP, "existing-group"], managed=False)
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["access_entry"]["kubernetesGroups"], ["existing-group"])
        self.assertIn("Deregistration is incomplete", result.stdout)

    def test_deregistration_preserves_existing_policies_and_reports_incomplete(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            access_entry=self.managed_entry(managed=False),
            policies=[
                {
                    "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy",
                    "accessScope": {"type": "cluster"},
                }
            ],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["access_entry"]["kubernetesGroups"], [])
        self.assertEqual(len(state["policies"]), 1)
        self.assertIn("Existing access policies were preserved", result.stdout)

    def test_deregistration_detects_actual_access_entry_username_binding(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            access_entry=self.managed_entry(username="preexisting-user", managed=False),
            extra_bindings=[role_binding_document("direct-user", user="preexisting-user")],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Other RBAC bindings still reference", result.stdout)
        self.assertIn("Deregistration is incomplete", result.stdout)

    def test_deregistration_reports_session_templated_username_as_unverifiable(self) -> None:
        self.configure_existing_default_rbac()
        templated_username = (
            f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/example/" + "{{SessionName}}"
        )
        self.fixture.update_state(
            access_entry=self.managed_entry(username=templated_username, managed=False),
            extra_bindings=[
                role_binding_document(
                    "expanded-session", user=f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/example/session-123"
                )
            ],
        )

        result = self.fixture.run("deregister-cluster.sh", input_text="y\n")
        state = self.fixture.read_state()

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(state["access_entry"])
        self.assertIn("session-templated username", result.stdout)
        self.assertIn("Deregistration is incomplete", result.stdout)

    def test_config_map_enrollment_is_idempotent_without_eksctl(self) -> None:
        self.configure_existing_default_rbac()
        self.fixture.update_state(
            authentication_mode="CONFIG_MAP",
            config_map_entries=[config_map_entry()],
        )

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(state["eksctl_calls"], [])
        self.assertIn("identity mapping already exists", result.stdout)

    def test_config_map_enrollment_retains_legacy_eksctl_path(self) -> None:
        self.fixture.close()
        self.fixture = ScriptFixture(include_eksctl=True)
        self.fixture.update_state(authentication_mode="CONFIG_MAP")

        result = self.fixture.run("enroll-cluster.sh")
        state = self.fixture.read_state()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(any(call.startswith("create iamidentitymapping") for call in state["eksctl_calls"]))
        self.assertFalse(any("create-access-entry" in call for call in state["aws_calls"]))


if __name__ == "__main__":
    unittest.main()
