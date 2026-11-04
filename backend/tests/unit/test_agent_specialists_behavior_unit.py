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
        """Test GameLift boto3 tools return the bounded, code-owned envelope."""
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
            # The raw FleetAttributes duplicate is gone; the model-facing
            # contract is ClassicFleets + ContainerFleets + FleetCounts.
            assert "FleetAttributes" not in result
            assert result["ClassicFleets"] == []
            assert result["ContainerFleets"] == []
            assert result["FleetCounts"] == {"Classic": 0, "Container": 0, "Total": 0}
            # A valid empty inventory with no failures is the distinct `empty`.
            assert result["status"] == "empty"
            # No fleets -> must not call describe_fleet_attributes
            mock_gamelift.describe_fleet_attributes.assert_not_called()

    def test_list_fleets_paginates_and_chunks(self):
        """list_gamelift_fleets pages fleets and chunks describe calls at 100.

        Regression for #124: a single list_fleets() call truncated large
        accounts. With 150 fleets across 2 pages, the projection item cap bounds
        the view to GAMELIFT_MAX_PROJECTED_ITEMS and marks it truncated, while
        the 100-ID chunking for describe_fleet_attributes is preserved on the
        capped ID set.
        """
        # Local modules
        from agents.gamelift_specialist import GAMELIFT_MAX_PROJECTED_ITEMS, list_gamelift_fleets

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
                "FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in FleetIds]
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            # View is item-capped and flagged truncated; counts never overclaim.
            assert len(result["ClassicFleets"]) == GAMELIFT_MAX_PROJECTED_ITEMS
            assert result["truncated"] is True
            assert result["FleetCounts"]["Classic"] == GAMELIFT_MAX_PROJECTED_ITEMS
            # describe called with <=100 IDs per call on the capped ID set.
            calls = mock_gamelift.describe_fleet_attributes.call_args_list
            assert all(len(c.kwargs["FleetIds"]) <= 100 for c in calls)

    def test_list_fleets_classic_only(self):
        """Classic fleets are projected when no container fleets exist."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": ["fleet-classic"]}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": []}])
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_fleet_attributes.return_value = {
                "FleetAttributes": [{"FleetId": "fleet-classic", "Status": "ACTIVE", "InstanceType": "c5.large"}]
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert "FleetAttributes" not in result
            assert result["ClassicFleets"] == [
                {"FleetId": "fleet-classic", "Status": "ACTIVE", "InstanceType": "c5.large"}
            ]
            assert result["ContainerFleets"] == []
            assert result["FleetCounts"] == {"Classic": 1, "Container": 0, "Total": 1}
            assert result["status"] == "ok"

    def test_list_fleets_container_only(self):
        """Container fleets are projected when classic list_fleets is empty."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": []}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": [{"FleetId": "containerfleet-1"}]}])
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
                                        "DeploymentStatus": "COMPLETE",
                                    }
                                ]
                            }
                        ]
                    )
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_container_fleet.return_value = {
                "ContainerFleet": {
                    "FleetId": "containerfleet-1",
                    "FleetArn": "arn:aws:gamelift:us-west-2:123456789012:containerfleet/cf-1",
                    "GameServerContainerGroupDefinitionName": "game-server-group",
                    "GameServerContainerGroupDefinitionArn": "container-group-definition/game-server-group:7",
                    "InstanceType": "c6i.large",
                    "BillingType": "ON_DEMAND",
                    "Status": "ACTIVE",
                    "DeploymentDetails": {"LatestDeploymentId": "deployment-one"},
                    "LogConfiguration": {"LogDestination": "CLOUDWATCH", "LogGroupArn": "log-group-arn"},
                    "LocationAttributes": [{"Location": "us-west-2", "Status": "ACTIVE"}],
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

            assert "FleetAttributes" not in result
            assert result["FleetCounts"] == {"Classic": 0, "Container": 1, "Total": 1}
            assert result["status"] == "ok"
            assert result["ContainerFleets"] == [
                {
                    "FleetType": "container",
                    "Status": "ACTIVE",
                    "InstanceType": "c6i.large",
                    "BillingType": "ON_DEMAND",
                    "GameServerContainerGroupDefinitionName": "game-server-group",
                    "GameServerContainerGroupDefinitionVersion": 7,
                    "DeploymentStatus": "COMPLETE",
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
            row = result["ContainerFleets"][0]
            assert "FleetId" not in row
            assert "FleetArn" not in row
            assert "LogGroupArn" not in row

    def test_list_fleets_mixed_classic_and_container(self):
        """Container IDs returned by list_fleets are not described as classic fleets."""
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": ["fleet-classic", "containerfleet-1"]}])
                if operation == "list_container_fleets":
                    return self._paginator([{"ContainerFleets": [{"FleetId": "containerfleet-1"}]}])
                if operation == "list_container_group_definitions":
                    return self._paginator([{"ContainerGroupDefinitions": []}])
                if operation == "list_fleet_deployments":
                    return self._paginator([{"FleetDeployments": []}])
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator

            def describe_fleet_attributes(FleetIds):
                assert FleetIds == ["fleet-classic"]
                return {"FleetAttributes": [{"FleetId": "fleet-classic", "Status": "ACTIVE"}]}

            mock_gamelift.describe_fleet_attributes.side_effect = describe_fleet_attributes
            mock_gamelift.describe_container_fleet.return_value = {
                "ContainerFleet": {
                    "FleetId": "containerfleet-1",
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

            assert result["ClassicFleets"] == [{"FleetId": "fleet-classic", "Status": "ACTIVE"}]
            assert result["ContainerFleets"][0]["FleetType"] == "container"
            assert result["ContainerFleets"][0]["GameServerContainerGroupDefinitionVersion"] == 3
            assert result["FleetCounts"] == {"Classic": 1, "Container": 1, "Total": 2}
            assert result["status"] == "ok"
            mock_gamelift.describe_fleet_attributes.assert_called_once()

    def test_list_fleets_container_api_failure_preserves_classic_results(self):
        """Classic fleet results still return when container APIs fail.

        The failed container listing becomes a code-owned {Source, Code} warning
        with no provider text, and the envelope is reported as a partial
        ``incomplete`` view that still carries the valid classic rows.
        """
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()

            def get_paginator(operation):
                if operation == "list_fleets":
                    return self._paginator([{"FleetIds": ["fleet-classic"]}])
                if operation == "list_container_fleets":
                    return self._paginator(error=Exception("container read denied"))
                raise AssertionError(f"Unexpected paginator: {operation}")

            mock_gamelift.get_paginator.side_effect = get_paginator
            mock_gamelift.describe_fleet_attributes.return_value = {
                "FleetAttributes": [{"FleetId": "fleet-classic", "Status": "ACTIVE"}]
            }
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert result["ClassicFleets"] == [{"FleetId": "fleet-classic", "Status": "ACTIVE"}]
            assert result["ContainerFleets"] == []
            assert result["FleetCounts"] == {"Classic": 1, "Container": 0, "Total": 1}
            # Code-owned, deduplicated {Source, Code, Count} warning only -- no provider text.
            assert result["Warnings"] == [{"Source": "container_fleets", "Code": "provider_error", "Count": 1}]
            assert "container read denied" not in __import__("json").dumps(result)
            assert result["status"] == "incomplete"
            assert result["partial"] is True


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
