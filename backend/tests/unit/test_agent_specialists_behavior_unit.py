"""
Unit tests for agent specialist behavioral logic - PURE UNIT TESTS
"""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


class TestGameLiftSpecialistBehavior:
    """Test GameLift specialist behavioral logic."""

    @staticmethod
    def _paginator(pages=None, error=None):
        paginator = MagicMock()
        if error:
            paginator.paginate.side_effect = error
        else:
            paginator.paginate.return_value = pages or []
        return paginator

    def test_gamelift_boto3_tools_exist(self):
        """Test GameLift boto3 fallback tools are defined."""
        # Local modules
        from agents.gamelift_specialist import (
            get_fleet_capacity,
            get_fleet_utilization,
            get_scaling_policies,
            list_gamelift_fleets,
        )

        # All should be callable
        assert callable(list_gamelift_fleets)
        assert callable(get_fleet_utilization)
        assert callable(get_fleet_capacity)
        assert callable(get_scaling_policies)

    def test_gamelift_boto3_tools_return_dict(self):
        """Test GameLift boto3 tools return dict structures."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        # Mock boto3 client (empty account: paginator yields a single empty page)
        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.get_paginator.side_effect = lambda operation: self._paginator(
                [{"FleetIds": []}] if operation == "list_fleets" else [{"ContainerFleets": []}]
            )
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert isinstance(result, dict)
            assert result["FleetAttributes"] == []
            assert result["ClassicFleets"] == []
            assert result["ContainerFleets"] == []
            assert result["FleetCounts"] == {"Classic": 0, "Container": 0, "Total": 0}
            # No fleets -> must not call describe_fleet_attributes
            mock_gamelift.describe_fleet_attributes.assert_not_called()

    def test_list_fleets_paginates_and_chunks(self):
        """list_gamelift_fleets pages all fleets and chunks describe calls at 100.

        Regression for #124: a single list_fleets() call truncated large
        accounts. With 150 fleets across 2 pages, all 150 must be described via
        two describe_fleet_attributes calls (100 + 50).
        """
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        page1 = [f"fleet-{i}" for i in range(100)]
        page2 = [f"fleet-{i}" for i in range(100, 150)]

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": page1}, {"FleetIds": page2}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": []}])
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_fleet_attributes.side_effect = lambda FleetIds: {
                "FleetAttributes": [{"FleetId": fid} for fid in FleetIds]
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            # All 150 fleets described, none dropped
            assert len(result["FleetAttributes"]) == 150
            # describe called twice with <=100 IDs each (100, then 50)
            calls = mock_gamelift.describe_fleet_attributes.call_args_list
            assert [len(c.kwargs["FleetIds"]) for c in calls] == [100, 50]

    def test_list_fleets_classic_only(self):
        """Classic fleets are preserved when no container fleets exist."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": ["classic-fleet"]}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": []}])
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_fleet_attributes.return_value = {
                "FleetAttributes": [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert result["FleetAttributes"] == [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            assert result["ClassicFleets"] == [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            assert result["ContainerFleets"] == []
            assert result["FleetCounts"] == {"Classic": 1, "Container": 0, "Total": 1}

    def test_list_fleets_container_only(self):
        """Container fleets are returned when classic list_fleets is empty."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": []}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": [{"FleetId": "container-fleet"}]}])
                if operation == "list_container_group_definitions":
                    return self._paginator(
                        [
                            {
                                "ContainerGroupDefinitions": [
                                    {
                                        "Name": "game-server-group",
                                        "VersionNumber": 7,
                                        "ContainerGroupType": "GAME_SERVER",
                                        "Status": "READY",
                                    }
                                ]
                            }
                        ]
                    )
                if operation == "list_fleet_deployments":
                    return self._paginator(
                        [
                            {
                                "FleetDeployments": [
                                    {
                                        "DeploymentId": "deployment-one",
                                        "DeploymentStatus": "COMPLETED",
                                    }
                                ]
                            }
                        ]
                    )
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_container_fleet.return_value = {
                "ContainerFleet": {
                    "FleetId": "container-fleet",
                    "FleetArn": "redacted-resource-reference",
                    "GameServerContainerGroupDefinitionName": "game-server-group",
                    "GameServerContainerGroupDefinitionArn": "container-group-definition/game-server-group:7",
                    "InstanceType": "c6i.large",
                    "BillingType": "ON_DEMAND",
                    "Status": "ACTIVE",
                    "DeploymentDetails": {"LatestDeploymentId": "deployment-one"},
                    "LogConfiguration": {"LogDestination": "CLOUDWATCH", "LogGroupArn": "log-group-arn"},
                    "LocationAttributes": [{"Location": "example-region", "Status": "ACTIVE"}],
                }
            }
            mock_gamelift.describe_container_group_definition.return_value = {
                "ContainerGroupDefinition": {
                    "Name": "game-server-group",
                    "VersionNumber": 7,
                    "ContainerGroupType": "GAME_SERVER",
                    "Status": "READY",
                    "OperatingSystem": "AMAZON_LINUX_2023",
                    "TotalMemoryLimitMebibytes": 1024,
                    "TotalVcpuLimit": 0.5,
                }
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert result["FleetAttributes"] == []
            assert result["FleetCounts"] == {"Classic": 0, "Container": 1, "Total": 1}
            assert result["ContainerFleets"] == [
                {
                    "FleetType": "container",
                    "Status": "ACTIVE",
                    "InstanceType": "c6i.large",
                    "BillingType": "ON_DEMAND",
                    "GameServerContainerGroupDefinitionName": "game-server-group",
                    "GameServerContainerGroupDefinitionVersion": 7,
                    "DeploymentStatus": "COMPLETED",
                    "LogDestinationType": "CLOUDWATCH",
                    "LocationCount": 1,
                    "ContainerGroupDefinition": {
                        "Name": "game-server-group",
                        "VersionNumber": 7,
                        "ContainerGroupType": "GAME_SERVER",
                        "Status": "READY",
                        "OperatingSystem": "AMAZON_LINUX_2023",
                        "TotalMemoryLimitMebibytes": 1024,
                        "TotalVcpuLimit": 0.5,
                    },
                }
            ]
            assert "FleetId" not in result["ContainerFleets"][0]
            assert "FleetArn" not in result["ContainerFleets"][0]
            assert "LogGroupArn" not in result["ContainerFleets"][0]

    def test_list_fleets_mixed_classic_and_container(self):
        """Classic and container fleets are both returned in distinct collections."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": ["classic-fleet"]}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": [{"FleetId": "container-fleet"}]}])
                if operation == "list_container_group_definitions":
                    return self._paginator([{"ContainerGroupDefinitions": []}])
                if operation == "list_fleet_deployments":
                    return self._paginator([{"FleetDeployments": []}])
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_fleet_attributes.return_value = {
                "FleetAttributes": [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            }
            mock_gamelift.describe_container_fleet.return_value = {
                "ContainerFleet": {
                    "FleetId": "container-fleet",
                    "GameServerContainerGroupDefinitionName": "game-server-group",
                    "GameServerContainerGroupDefinitionArn": "container-group-definition/game-server-group:3",
                    "InstanceType": "c6i.large",
                    "Status": "ACTIVE",
                    "LogConfiguration": {"LogDestination": "NONE"},
                }
            }
            mock_gamelift.describe_container_group_definition.return_value = {
                "ContainerGroupDefinition": {
                    "Name": "game-server-group",
                    "VersionNumber": 3,
                    "Status": "READY",
                }
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert result["ClassicFleets"] == [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            assert result["ContainerFleets"][0]["FleetType"] == "container"
            assert result["ContainerFleets"][0]["GameServerContainerGroupDefinitionVersion"] == 3
            assert result["FleetCounts"] == {"Classic": 1, "Container": 1, "Total": 2}

    def test_list_fleets_container_api_failure_preserves_classic_results(self):
        """Classic fleet results still return when container APIs fail."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": ["classic-fleet"]}])
                if operation == "list_container_fleets":
                    return self._paginator(error=Exception("container read denied"))
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_fleet_attributes.return_value = {
                "FleetAttributes": [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert result["ClassicFleets"] == [{"FleetId": "classic-fleet", "Status": "ACTIVE"}]
            assert result["ContainerFleets"] == []
            assert result["FleetCounts"] == {"Classic": 1, "Container": 0, "Total": 1}
            assert result["Warnings"] == [{"Source": "container_fleets", "Message": "container read denied"}]
            assert "error" not in result


class TestEKSSpecialistBehavior:
    """Test EKS specialist behavioral logic."""

    def test_eks_fallback_returns_string(self):
        """Test EKS fallback returns string."""
        # Local modules
        from agents.eks_specialist import _get_eks_aws_cli_fallback

        result = _get_eks_aws_cli_fallback("us-west-2")

        assert isinstance(result, str)
        assert "aws eks" in result.lower()
        assert "us-west-2" in result

    def test_eks_fallback_contains_useful_commands(self):
        """Test EKS fallback contains useful commands."""
        # Local modules
        from agents.eks_specialist import _get_eks_aws_cli_fallback

        result = _get_eks_aws_cli_fallback("us-west-2")

        assert "list-clusters" in result
        assert "describe-cluster" in result


class TestCostSpecialistBehavior:
    """Test Cost specialist behavioral logic."""

    def test_cost_fallback_returns_string(self):
        """Test Cost fallback returns string."""
        # Local modules
        from agents.cost_specialist import _get_cost_aws_cli_fallback

        result = _get_cost_aws_cli_fallback("us-west-2")

        assert isinstance(result, str)
        assert "aws ce" in result.lower()
        assert "us-west-2" in result  # Uses the region parameter

    def test_cost_fallback_contains_useful_commands(self):
        """Test Cost fallback contains useful commands."""
        # Local modules
        from agents.cost_specialist import _get_cost_aws_cli_fallback

        result = _get_cost_aws_cli_fallback("us-west-2")

        assert "get-cost-and-usage" in result
        assert "get-rightsizing-recommendation" in result


class TestAgentSpecialistErrorHandling:
    """Test agent error handling - pure function tests only."""

    def test_eks_and_cost_fallback_functions_accept_region_parameter(self):
        """Test EKS and Cost fallback functions accept region parameter."""
        # Local modules
        from agents.cost_specialist import _get_cost_aws_cli_fallback as cost_fallback
        from agents.eks_specialist import _get_eks_aws_cli_fallback as eks_fallback

        # All should accept region parameter without error
        eks_result = eks_fallback("us-east-1")
        cost_result = cost_fallback("us-east-1")

        assert isinstance(eks_result, str)
        assert isinstance(cost_result, str)

    def test_gamelift_boto3_tools_handle_errors(self):
        """Test GameLift boto3 tools handle errors gracefully."""
        # Standard library
        from unittest.mock import patch

        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        # Mock boto3 to raise exception
        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_client.side_effect = Exception("Connection failed")

            result = list_gamelift_fleets()

            # Should return dict with error, not raise exception
            assert isinstance(result, dict)
            assert "error" in result
