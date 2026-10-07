"""Structure checks for the Agones-to-GameLift reference environment manifests.

The demo under demo/agones-gamelift-migration is operator-run and never deployed
by the product. These checks pin its safety boundary without any cloud access:
read-only assistant RBAC, a private and source-restricted cluster, internal-only
Agones services, and a region-bound, resource-scoped operator role.
"""

# Standard library
from pathlib import Path
from typing import Any

# Third-party packages
import pytest
import yaml

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

_DEMO = Path(__file__).resolve().parents[3] / "demo" / "agones-gamelift-migration"


def _docs(relative: str) -> list[Any]:
    return [doc for doc in yaml.safe_load_all((_DEMO / relative).read_text(encoding="utf-8")) if doc]


def test_agent_rbac_is_read_only_and_excludes_secrets():
    docs = _docs("agones/agent-readonly-rbac.yaml")
    roles = [d for d in docs if d["kind"] == "ClusterRole"]
    assert len(roles) == 1
    for rule in roles[0]["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
        assert "secrets" not in rule["resources"]
        assert "*" not in rule["resources"] + rule["verbs"] + rule["apiGroups"]
    (binding,) = [d for d in docs if d["kind"] == "ClusterRoleBinding"]
    assert binding["roleRef"]["name"] == roles[0]["metadata"]["name"]
    assert [s["kind"] for s in binding["subjects"]] == ["Group"]


def test_cluster_is_private_source_restricted_and_tagged():
    (config,) = _docs("eks/cluster.yaml")
    assert config["metadata"]["version"] == "1.35"
    assert config["vpc"]["publicAccessCIDRs"] == ["__OPERATOR_CIDR__"]
    assert config["vpc"]["clusterEndpoints"]["privateAccess"] is True
    assert config["vpc"]["nat"]["gateway"] == "Single"
    for group in config["managedNodeGroups"]:
        assert group["privateNetworking"] is True
        assert group["disableIMDSv1"] is True
        assert group["maxSize"] <= 2
    for tags in [config["metadata"]["tags"], *(g["tags"] for g in config["managedNodeGroups"])]:
        assert tags["demo"] == "agones-gamelift-migration"
        assert tags["demo-env"] == "__CLUSTER_NAME__"
        assert {"owner", "expires-at"} <= set(tags)


def test_agones_services_stay_cluster_internal():
    (values,) = _docs("agones/helm-values.yaml")
    agones = values["agones"]
    assert agones["allocator"]["service"]["serviceType"] == "ClusterIP"
    assert agones["ping"]["http"]["serviceType"] == "ClusterIP"
    assert agones["ping"]["udp"]["serviceType"] == "ClusterIP"


def test_load_job_is_bounded():
    (job,) = _docs("client/load-job.yaml")
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def _statements() -> list[dict[str, Any]]:
    template = load_cfn_template((_DEMO / "iam" / "operator-role.yaml").read_text(encoding="utf-8"))
    role = template["Resources"]["OperatorRole"]["Properties"]
    assert role["MaxSessionDuration"] == 3600
    return role["Policies"][0]["PolicyDocument"]["Statement"]


def _actions(statement: dict[str, Any]) -> list[str]:
    actions = statement.get("Action", statement.get("NotAction", []))
    return [actions] if isinstance(actions, str) else actions


def test_operator_role_is_bound_to_one_region():
    deny = next(s for s in _statements() if s["Sid"] == "DenyOtherRegions")
    assert deny["Effect"] == "Deny"
    assert deny["Resource"] == "*"
    assert set(deny["NotAction"]) == {"iam:*", "sts:*"}
    assert "aws:RequestedRegion" in deny["Condition"]["StringNotEquals"]


def test_operator_role_grants_no_wildcard_or_unscoped_iam():
    for statement in _statements():
        if statement["Effect"] != "Allow":
            continue
        actions = _actions(statement)
        assert "*" not in actions
        assert not any(a in {"iam:*", "sts:*", "sts:AssumeRole"} for a in actions), statement["Sid"]
        iam_writes = [a for a in actions if a.startswith("iam:") and not a.startswith(("iam:Get", "iam:List"))]
        if iam_writes and statement["Sid"] != "ServiceLinkedRoles":
            assert statement["Resource"] != "*", statement["Sid"]
    linked = next(s for s in _statements() if s["Sid"] == "ServiceLinkedRoles")
    assert "iam:AWSServiceName" in linked["Condition"]["StringEquals"]


def test_operator_role_passes_roles_only_to_demo_services():
    passrole = next(s for s in _statements() if s["Sid"] == "PassDemoRolesToTheirServices")
    assert set(passrole["Condition"]["StringEquals"]["iam:PassedToService"]) == {
        "eks.amazonaws.com",
        "ec2.amazonaws.com",
        "gamelift.amazonaws.com",
    }


def test_operator_role_cannot_touch_other_stacks_clusters_or_repositories():
    by_sid = {s["Sid"]: s for s in _statements()}
    for sid in ("CloudFormationDemoStacks", "EksDemoCluster", "EcrDemoRepository"):
        resources = by_sid[sid]["Resource"]
        resources = [resources] if not isinstance(resources, list) else resources
        assert all(r != "*" for r in resources), sid
