"""Infrastructure contract tests for the shared cost report snapshot store (#365).

Assert the DynamoDB table encryption/TTL, least-privilege IAM (GetItem/PutItem
only, no scans, no delete), the stack output, and the deploy-time environment
wiring that passes the table name and trusted identity into the runtime.
"""

# Standard library
import pathlib

# Third-party packages
import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
BASE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml"
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/deploy.sh"


class CloudFormationLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves CloudFormation intrinsic values."""


def _construct_intrinsic(loader, _tag_suffix, node):
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"Unsupported CloudFormation YAML node: {type(node).__name__}")


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.load(BASE_TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)


def _table(template: dict) -> dict:
    return template["Resources"]["CostReportSnapshotTable"]["Properties"]


def test_table_is_on_demand_with_report_id_key(template):
    props = _table(template)
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["AttributeDefinitions"] == [{"AttributeName": "reportId", "AttributeType": "S"}]
    assert props["KeySchema"] == [{"AttributeName": "reportId", "KeyType": "HASH"}]


def test_table_enables_ttl_on_expires_at(template):
    ttl = _table(template)["TimeToLiveSpecification"]
    assert ttl["AttributeName"] == "expiresAt"
    assert ttl["Enabled"] is True


def test_table_enables_server_side_encryption(template):
    assert _table(template)["SSESpecification"]["SSEEnabled"] is True


def test_table_is_tagged_with_project_conventions(template):
    tags = {tag["Key"]: tag["Value"] for tag in _table(template)["Tags"]}
    # Uses the repo's Project/Purpose/ManagedBy convention. No misleading
    # hard-coded environment/owner/cost-center tags.
    assert set(tags) == {"Project", "Purpose", "ManagedBy"}
    assert tags["Purpose"] == "cost-report-snapshots"
    assert tags["ManagedBy"] == "cloudformation"
    for forbidden in ("Environment", "Owner", "CostCenter"):
        assert forbidden not in tags


def _snapshot_policy(template: dict) -> dict:
    policies = template["Resources"]["AgentCoreExecutionRole"]["Properties"]["Policies"]
    matches = [p for p in policies if p["PolicyName"] == "CostReportSnapshotStoreAccess"]
    assert matches, "CostReportSnapshotStoreAccess policy not found"
    return matches[0]


def test_runtime_iam_is_get_and_put_only(template):
    statement = _snapshot_policy(template)["PolicyDocument"]["Statement"][0]
    assert sorted(statement["Action"]) == ["dynamodb:GetItem", "dynamodb:PutItem"]
    assert statement["Resource"] == ["CostReportSnapshotTable.Arn"]


def test_runtime_iam_grants_no_scan_query_or_delete(template):
    policy_text = yaml.dump(_snapshot_policy(template))
    for forbidden in ("dynamodb:Scan", "dynamodb:Query", "dynamodb:DeleteItem", "dynamodb:*"):
        assert forbidden not in policy_text


def test_stack_outputs_table_name(template):
    output = template["Outputs"]["CostReportSnapshotTableName"]
    assert output["Value"] == "CostReportSnapshotTable"  # !Ref resolves to logical id via loader
    assert output["Export"]["Name"] == "${ProjectName}-CostReportSnapshotTableName"


def test_deploy_script_wires_table_and_identity_into_runtime_env():
    bash = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "CostReportSnapshotTableName" in bash
    assert 'append_agentcore_env_if_resolved "GBAW_COST_SNAPSHOT_TABLE_NAME"' in bash
    assert 'append_agentcore_env_if_resolved "GBAW_COST_SNAPSHOT_REQUIRED"' in bash
    assert 'append_agentcore_env_if_resolved "GBAW_COST_SNAPSHOT_TTL_SECONDS"' in bash
    assert 'append_agentcore_env_if_resolved "GBAW_TENANT_ID"' in bash
    assert 'append_agentcore_env_if_resolved "GBAW_WORKSPACE_ID"' in bash


def test_deploy_script_fails_closed_when_table_output_missing():
    bash = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # A missing snapshot-table output must abort the deployment rather than
    # silently degrading to a process-local cache.
    assert "Base stack did not export CostReportSnapshotTableName" in bash
    assert "COST_SNAPSHOT_REQUIRED=true" in bash


def test_deploy_script_validates_bounded_ttl():
    bash = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "GBAW_COST_SNAPSHOT_TTL_SECONDS" in bash
    assert "[60, 86400]" in bash
