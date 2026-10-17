"""Contract tests for deploying the frontend without a default VPC.

ECS Express falls back to the default VPC, which landing-zone accounts often do
not have. The frontend template must accept subnets, and both deploy paths must
supply them from configuration, the default VPC, or a small public VPC.
"""

# Standard library
import pathlib

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
CFN = PROJECT_ROOT / "infrastructure/cloudformation"
DEPLOY = PROJECT_ROOT / "scripts/deploy.sh"
TEARDOWN = PROJECT_ROOT / "scripts/teardown.sh"
PS_DEPLOY = PROJECT_ROOT / "scripts/powershell/Public/Deploy-GameAgent.ps1"
PS_REMOVE = PROJECT_ROOT / "scripts/powershell/Public/Remove-GameAgent.ps1"


def _load(name: str) -> dict:
    return load_cfn_template((CFN / name).read_text(encoding="utf-8"))


def test_frontend_takes_optional_subnets_and_defaults_to_the_default_vpc():
    template = _load("02-frontend-ecs-express.yaml")

    parameter = template["Parameters"]["SubnetIds"]
    assert parameter["Type"] == "CommaDelimitedList"
    assert parameter["Default"] == ""
    network = template["Resources"]["FrontendService"]["Properties"]["NetworkConfiguration"]
    assert network == ["HasSubnetIds", {"Subnets": "SubnetIds"}, "AWS::NoValue"]


def test_network_stack_has_two_public_subnets_in_distinct_zones():
    resources = _load("06-frontend-network.yaml")["Resources"]

    subnets = [resources["PublicSubnetA"]["Properties"], resources["PublicSubnetB"]["Properties"]]
    assert [subnet["AvailabilityZone"][0] for subnet in subnets] == [0, 1]
    assert all(subnet["MapPublicIpOnLaunch"] is False for subnet in subnets)

    route = resources["PublicDefaultRoute"]["Properties"]
    assert route["DestinationCidrBlock"] == "0.0.0.0/0"
    assert route["GatewayId"] == "InternetGateway"
    associations = {
        resources[name]["Properties"]["SubnetId"]
        for name in ("PublicSubnetARouteTableAssociation", "PublicSubnetBRouteTableAssociation")
    }
    assert associations == {"PublicSubnetA", "PublicSubnetB"}


def test_network_stack_creates_no_nat_gateway_or_security_groups():
    resources = _load("06-frontend-network.yaml")["Resources"]

    types = {resource["Type"] for resource in resources.values()}
    assert "AWS::EC2::NatGateway" not in types
    assert "AWS::EC2::SecurityGroup" not in types
    assert "AWS::EC2::EIP" not in types


def test_bash_deploy_and_teardown_handle_the_frontend_network():
    deploy = DEPLOY.read_text(encoding="utf-8")
    teardown = TEARDOWN.read_text(encoding="utf-8")

    assert "GBAW_FRONTEND_SUBNET_IDS" in deploy
    assert "Name=is-default,Values=true" in deploy
    assert "06-frontend-network.yaml" in deploy
    assert 'SubnetIds="$FRONTEND_SUBNET_IDS"' in deploy
    assert teardown.index("${PROJECT_NAME}-frontend-network") > teardown.index(
        "delete-stack --stack-name ${PROJECT_NAME}-frontend "
    )


def test_powershell_deploy_and_teardown_handle_the_frontend_network():
    deploy = PS_DEPLOY.read_text(encoding="utf-8")
    remove = PS_REMOVE.read_text(encoding="utf-8")

    assert "GBAW_FRONTEND_SUBNET_IDS" in deploy
    assert "06-frontend-network.yaml" in deploy
    assert '"SubnetIds=$frontendSubnetIds"' in deploy
    assert remove.index('Remove-Stack "$ProjectName-frontend-network"') > remove.index(
        'Remove-Stack "$ProjectName-frontend"'
    )
