"""Structure checks for the Agones-to-GameLift reference environment manifests.

The demo under demo/agones-gamelift-migration is operator-run and never deployed
by the product. These checks pin its safety boundary without any cloud access:
read-only assistant RBAC, a private and source-restricted cluster, internal-only
Agones services, and a region-bound, resource-scoped operator role.
"""

# Standard library
import re
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


def test_cluster_roles_carry_the_workload_boundary():
    (config,) = _docs("eks/cluster.yaml")
    assert config["iam"]["serviceRolePermissionsBoundary"] == "__BOUNDARY_ARN__"
    for group in config["managedNodeGroups"]:
        assert group["iam"]["instanceRolePermissionsBoundary"] == "__BOUNDARY_ARN__"


def test_load_job_is_bounded_and_unprivileged():
    (job,) = _docs("client/load-job.yaml")
    spec = job["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] > 0
    assert spec["ttlSecondsAfterFinished"] > 0
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    for container in pod["containers"]:
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False
        assert context["capabilities"]["drop"] == ["ALL"]
        assert {"cpu", "memory"} <= set(container["resources"]["limits"])


def test_fleet_game_server_is_unprivileged():
    (fleet,) = _docs("agones/fleet.yaml")
    for container in fleet["spec"]["template"]["spec"]["template"]["spec"]["containers"]:
        context = container["securityContext"]
        assert context["runAsNonRoot"] is True
        assert context["allowPrivilegeEscalation"] is False
        assert context["capabilities"]["drop"] == ["ALL"]


def _operator_template() -> dict[str, Any]:
    return load_cfn_template((_DEMO / "iam" / "operator-role.yaml").read_text(encoding="utf-8"))


def _statements() -> list[dict[str, Any]]:
    role = _operator_template()["Resources"]["OperatorRole"]["Properties"]
    assert role["MaxSessionDuration"] == 3600
    return role["Policies"][0]["PolicyDocument"]["Statement"]


def _by_sid() -> dict[str, dict[str, Any]]:
    return {s["Sid"]: s for s in _statements()}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


_DEMO_ROLE_PATTERNS = {
    "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/eksctl-${ClusterName}-*",
    "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${StackPrefix}-*",
}
_CODE_EXECUTION_ACTIONS = {
    "ec2:ModifyInstanceAttribute",
    "ec2:ModifySnapshotAttribute",
    "ec2:ModifyImageAttribute",
    "ec2:CreateSnapshot",
    "ec2:CreateSnapshots",
    "ec2:GetPasswordData",
}


def test_role_creation_and_policy_changes_require_the_workload_boundary():
    by_sid = _by_sid()
    role_writes = {"iam:CreateRole", "iam:PutRolePermissionsBoundary", "iam:AttachRolePolicy", "iam:PutRolePolicy"}
    for statement in _statements():
        if statement["Effect"] != "Allow":
            continue
        granted = role_writes & set(_actions(statement))
        if not granted:
            continue
        assert statement["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == "WorkloadBoundary", statement["Sid"]
        assert set(_list(statement["Resource"])) <= _DEMO_ROLE_PATTERNS, statement["Sid"]
    deny = by_sid["DenyOtherBoundaries"]
    assert deny["Effect"] == "Deny"
    assert {"iam:CreateRole", "iam:PutRolePermissionsBoundary"} <= set(_actions(deny))
    assert deny["Condition"]["StringNotEquals"]["iam:PermissionsBoundary"] == "WorkloadBoundary"
    removal = by_sid["DenyBoundaryRemovalOrReplacement"]
    assert removal["Effect"] == "Deny" and _actions(removal) == ["iam:DeleteRolePermissionsBoundary"]
    assert removal["Resource"] == "*"


def test_attachable_policies_are_an_allowlist():
    attach = _by_sid()["IamAttachAllowlistedPolicies"]
    assert _actions(attach) == ["iam:AttachRolePolicy"]
    allowed = set(attach["Condition"]["ArnLike"]["iam:PolicyARN"])
    managed = {a.rsplit("/", 1)[1] for a in allowed if ":iam::aws:policy/" in a}
    assert managed == {
        "AmazonEKSClusterPolicy",
        "AmazonEKSVPCResourceController",
        "AmazonEKSWorkerNodePolicy",
        "AmazonEKS_CNI_Policy",
        "AmazonEC2ContainerRegistryReadOnly",
        "AmazonEC2ContainerRegistryPullOnly",
        "AmazonSSMManagedInstanceCore",
        "GameLiftContainerFleetPolicy",
    }
    customer = allowed - {a for a in allowed if ":iam::aws:policy/" in a}
    assert customer == {"arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/eksctl-${ClusterName}-*"}
    assert not any("*" in a.rsplit("/", 1)[1] for a in allowed - customer)


def test_inline_policies_only_on_eksctl_roles():
    for statement in _statements():
        if statement["Effect"] == "Allow" and "iam:PutRolePolicy" in _actions(statement):
            resources = set(_list(statement["Resource"]))
            assert resources == {"arn:${AWS::Partition}:iam::${AWS::AccountId}:role/eksctl-${ClusterName}-*"}


def test_operator_role_cannot_change_protected_identities():
    deny = _by_sid()["DenyChangesToProtectedIdentities"]
    assert deny["Effect"] == "Deny"
    assert {"iam:Attach*", "iam:Put*", "iam:Delete*", "iam:PassRole"} <= set(_actions(deny))
    resources = _list(deny["Resource"])
    assert "WorkloadBoundary" in resources
    subs = set(resources)
    assert "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${AgentRoleName}" in subs
    assert "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${ClusterName}-operator" in subs


def test_code_execution_ec2_actions_are_denied_for_operator_and_workloads():
    operator = _by_sid()["DenyCodeExecutionAsOtherRoles"]
    assert operator["Effect"] == "Deny" and operator["Resource"] == "*"
    assert _CODE_EXECUTION_ACTIONS | {
        "ec2:CreateVpcPeeringConnection",
        "ec2:CreateVpnConnection",
        "ec2:CreateTransitGatewayVpcAttachment",
    } <= set(_actions(operator))
    boundary = _operator_template()["Resources"]["WorkloadBoundary"]["Properties"]["PolicyDocument"]["Statement"]
    deny = next(s for s in boundary if s["Effect"] == "Deny")
    assert _CODE_EXECUTION_ACTIONS <= set(_actions(deny))
    allowed = {a for s in boundary if s["Effect"] == "Allow" for a in _actions(s)}
    assert not any(a.startswith(("iam:", "sts:")) for a in allowed - {"iam:CreateServiceLinkedRole"})
    assert "*" not in allowed and "ssm:SendCommand" not in allowed and "s3:GetObject" not in allowed


def test_gamelift_writes_are_scoped_by_name_or_demo_env_tag():
    prefixed = {
        "arn:${AWS::Partition}:gamelift:${AWS::Region}:${AWS::AccountId}:containergroupdefinition/${StackPrefix}-*",
        "arn:${AWS::Partition}:gamelift:${AWS::Region}:${AWS::AccountId}:gamesessionqueue/${StackPrefix}-*",
    }
    for statement in _statements():
        actions = [a for a in _actions(statement) if a.startswith("gamelift:")]
        if statement["Effect"] != "Allow" or not actions or statement["Sid"] == "GameLiftReads":
            continue
        if statement["Sid"] == "GameLiftStartTestSessions":
            # Not scopable (no resource type or condition keys); nothing else may ride along.
            assert _actions(statement) == ["gamelift:CreateGameSession"]
            continue
        condition = statement.get("Condition", {}).get("StringEquals", {})
        tagged = bool(set(condition) & {"aws:RequestTag/demo-env", "aws:ResourceTag/demo-env"})
        if tagged:
            assert all(v == "ClusterName" for v in condition.values()), statement["Sid"]
        else:
            assert set(_list(statement["Resource"])) <= prefixed, statement["Sid"]
        top_level_creates = {
            "gamelift:CreateContainerFleet",
            "gamelift:CreateContainerGroupDefinition",
            "gamelift:CreateGameSessionQueue",
        }
        if top_level_creates & set(actions):
            assert condition.get("aws:RequestTag/demo-env") == "ClusterName", statement["Sid"]
    fleets = _by_sid()["GameLiftChangeTaggedFleetsOnly"]
    assert fleets["Condition"]["StringEquals"]["aws:ResourceTag/demo-env"] == "ClusterName"
    assert {"gamelift:DeleteContainerFleet", "gamelift:UpdateContainerFleet"} <= set(_actions(fleets))
    reads_actions = _actions(_by_sid()["GameLiftReads"])
    assert all(a.split(":")[1].startswith(("Describe", "List")) for a in reads_actions)


def test_reference_fleet_is_closed_by_default_bounded_and_tagged():
    template = load_cfn_template((_DEMO / "gamelift" / "container-fleet.reference.yaml").read_text(encoding="utf-8"))
    parameters = template["Parameters"]
    source = parameters["PlayerSourceCidr"]
    assert "Default" not in source
    pattern = re.compile(source["AllowedPattern"])
    for accepted in ("198.51.100.7/32", "203.0.113.16/28"):
        assert pattern.fullmatch(accepted), accepted
    for rejected in ("0.0.0.0/0", "10.0.0.0/8", "198.51.100.0/24", "198.51.100.7", "::/0", "256.1.1.1/32"):
        assert not pattern.fullmatch(rejected), rejected
    resources = template["Resources"]
    fleet = resources["ContainerFleet"]["Properties"]
    ports = fleet["InstanceConnectionPortRange"]
    (permission,) = fleet["InstanceInboundPermissions"]
    assert permission["IpRange"] == "PlayerSourceCidr"
    assert (permission["FromPort"], permission["ToPort"]) == (ports["FromPort"], ports["ToPort"])
    assert permission["Protocol"] == "UDP"
    assert fleet["Locations"][0]["LocationCapacity"]["MaxSize"] <= 3
    assert resources["FleetRole"]["Properties"]["PermissionsBoundary"] == "BoundaryArn"
    for name in ("FleetRole", "GameServerContainerGroup", "ContainerFleet", "GameSessionQueue"):
        tags = {t["Key"]: t["Value"] for t in resources[name]["Properties"]["Tags"]}
        assert tags == {"demo": "agones-gamelift-migration", "demo-env": "DemoEnv"}, name


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


# ---------------------------------------------------------------------------
# #608: exact resource scopes, tagging, boundary ceiling, fleet logs
# ---------------------------------------------------------------------------

_P = "arn:${AWS::Partition}"
_ACCT = "${AWS::Region}:${AWS::AccountId}"
_IAM = f"{_P}:iam::${{AWS::AccountId}}"
_DEMO_ROLES = [f"{_IAM}:role/eksctl-${{ClusterName}}-*", f"{_IAM}:role/${{StackPrefix}}-*"]

# Every Allow statement's exact Resource list. Widening any scope (for example
# PassRole to role/*, ECR to repository/*, or CloudFormation to stack/*), or
# adding a statement, must change this pin.
EXPECTED_ALLOW_RESOURCES = {
    "Ec2ForEksctl": ["*"],
    "CloudFormationDemoStacks": [
        f"{_P}:cloudformation:{_ACCT}:stack/eksctl-${{ClusterName}}-*/*",
        f"{_P}:cloudformation:{_ACCT}:stack/${{StackPrefix}}-*/*",
    ],
    "CloudFormationReadAndValidate": ["*"],
    "EksDemoCluster": [
        f"{_P}:eks:{_ACCT}:cluster/${{ClusterName}}",
        f"{_P}:eks:{_ACCT}:nodegroup/${{ClusterName}}/*/*",
        f"{_P}:eks:{_ACCT}:addon/${{ClusterName}}/*/*",
        f"{_P}:eks:{_ACCT}:access-entry/${{ClusterName}}/*",
        f"{_P}:eks:{_ACCT}:podidentityassociation/${{ClusterName}}/*",
        f"{_P}:eks::aws:cluster-access-policy/*",
    ],
    "EksAccountReads": ["*"],
    "EksAmiLookup": [f"{_P}:ssm:${{AWS::Region}}::parameter/aws/*"],
    "EksctlLogRetention": [f"{_P}:logs:{_ACCT}:log-group:/aws/eks/${{ClusterName}}/*"],
    "FleetLogGroups": [
        f"{_P}:logs:{_ACCT}:log-group:gamelift-${{StackPrefix}}-*",
        f"{_P}:logs:{_ACCT}:log-group:gamelift-containerfleet-*",
    ],
    "ListLogGroups": ["*"],
    "IamCreateBoundedRoles": _DEMO_ROLES,
    "IamAttachAllowlistedPolicies": _DEMO_ROLES,
    "IamInlinePoliciesOnEksctlRolesOnly": [f"{_IAM}:role/eksctl-${{ClusterName}}-*"],
    "IamManageDemoRoles": [
        f"{_IAM}:role/eksctl-${{ClusterName}}-*",
        f"{_IAM}:instance-profile/eksctl-${{ClusterName}}-*",
        f"{_IAM}:role/${{StackPrefix}}-*",
    ],
    "PassDemoRolesToTheirServices": _DEMO_ROLES,
    "ReadAgentRole": [f"{_IAM}:role/${{AgentRoleName}}"],
    "ReadEksServiceLinkedRoles": [
        f"{_IAM}:role/aws-service-role/eks.amazonaws.com/*",
        f"{_IAM}:role/aws-service-role/eks-nodegroup.amazonaws.com/*",
    ],
    "EksctlManagedPolicies": [f"{_IAM}:policy/eksctl-${{ClusterName}}-*"],
    "IamReadPolicies": [f"{_P}:iam::aws:policy/*", "WorkloadBoundary"],
    "ServiceLinkedRoles": ["*"],
    "GameLiftCreateTagged": ["*"],
    "GameLiftTagThisEnvironmentOnly": ["*"],
    "GameLiftChangeNamedOnly": [
        f"{_P}:gamelift:{_ACCT}:containergroupdefinition/${{StackPrefix}}-*",
        f"{_P}:gamelift:{_ACCT}:gamesessionqueue/${{StackPrefix}}-*",
    ],
    "GameLiftChangeTaggedFleetsOnly": [
        f"{_P}:gamelift:{_ACCT}:containerfleet/*",
        f"{_P}:gamelift:{_ACCT}:fleet/*",
    ],
    "GameLiftStartTestSessions": ["*"],
    "GameLiftReads": ["*"],
    "EcrDemoRepository": [f"{_P}:ecr:{_ACCT}:repository/${{RepositoryName}}"],
    "EcrLogin": ["*"],
    "ResourceDiscoveryForCleanupCheck": ["*"],
}


def test_every_allow_statement_has_its_exact_resource_scope():
    allows = {s["Sid"]: _list(s["Resource"]) for s in _statements() if s["Effect"] == "Allow"}
    assert allows == EXPECTED_ALLOW_RESOURCES


def test_no_policy_version_actions_are_granted():
    """A new policy version would rewrite an attached eksctl policy in place, outside the boundary checks."""
    forbidden = {"iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion"}
    for statement in _statements():
        if statement["Effect"] != "Allow":
            continue
        actions = set(_actions(statement))
        assert not actions & forbidden, statement["Sid"]
        assert not any(a.endswith("*") and a.startswith("iam:") for a in actions), statement["Sid"]


def test_tagging_cannot_bring_another_environments_resource_into_scope():
    tagging = [s for s in _statements() if s["Effect"] == "Allow" and "gamelift:TagResource" in _actions(s)]
    assert [s["Sid"] for s in tagging] == ["GameLiftTagThisEnvironmentOnly"]
    (statement,) = tagging
    assert _actions(statement) == ["gamelift:TagResource"]
    assert statement["Condition"] == {
        "StringEquals": {"aws:RequestTag/demo-env": "ClusterName"},
        "StringEqualsIfExists": {"aws:ResourceTag/demo-env": "ClusterName"},
    }


def test_workload_boundary_is_region_bound_and_reads_only_this_cluster():
    boundary = _operator_template()["Resources"]["WorkloadBoundary"]["Properties"]["PolicyDocument"]["Statement"]
    by_sid = {s["Sid"]: s for s in boundary}
    region = by_sid["DenyOtherRegions"]
    assert region["Effect"] == "Deny" and region["Resource"] == "*"
    assert set(region["NotAction"]) == {"iam:*", "sts:*"}
    assert region["Condition"] == {"StringNotEquals": {"aws:RequestedRegion": "AWS::Region"}}
    eks = [a for s in boundary if s["Effect"] == "Allow" for a in _actions(s) if a.startswith(("eks:", "eks-auth:"))]
    assert sorted(eks) == ["eks-auth:AssumeRoleForPodIdentity", "eks:DescribeCluster"]
    assert by_sid["EksThisClusterOnly"]["Resource"] == f"{_P}:eks:{_ACCT}:cluster/${{ClusterName}}"


def test_reference_fleet_logs_to_a_group_the_stack_deletes():
    template = load_cfn_template((_DEMO / "gamelift" / "container-fleet.reference.yaml").read_text(encoding="utf-8"))
    resources = template["Resources"]
    group = resources["FleetLogGroup"]
    assert group["Type"] == "AWS::Logs::LogGroup"
    assert group["DeletionPolicy"] == "Delete" and group["UpdateReplacePolicy"] == "Delete"
    assert group["Properties"]["LogGroupName"] == "gamelift-${AWS::StackName}"
    assert 1 <= group["Properties"]["RetentionInDays"] <= 30
    tags = {t["Key"]: t["Value"] for t in group["Properties"]["Tags"]}
    assert tags == {"demo": "agones-gamelift-migration", "demo-env": "DemoEnv"}
    logging = resources["ContainerFleet"]["Properties"]["LogConfiguration"]
    assert logging["LogDestination"] == "CLOUDWATCH"
    assert logging["LogGroupArn"].endswith(":log-group:${FleetLogGroup}")
