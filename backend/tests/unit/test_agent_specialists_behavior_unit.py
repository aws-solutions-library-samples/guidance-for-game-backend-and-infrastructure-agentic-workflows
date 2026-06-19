"""
Unit tests for agent specialist behavioral logic - PURE UNIT TESTS
"""

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


class TestGameLiftSpecialistBehavior:
    """Test GameLift specialist behavioral logic."""

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
        # Standard library
        from unittest.mock import MagicMock, patch

        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        # Mock boto3 client (empty account: paginator yields a single empty page)
        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.get_paginator.return_value.paginate.return_value = [{"FleetIds": []}]
            mock_client.return_value = mock_gamelift

            result = list_gamelift_fleets()

            assert isinstance(result, dict)
            assert result["FleetAttributes"] == []
            # No fleets -> must not call describe_fleet_attributes
            mock_gamelift.describe_fleet_attributes.assert_not_called()

    def test_list_fleets_paginates_and_chunks(self):
        """list_gamelift_fleets pages all fleets and chunks describe calls at 100.

        Regression for #124: a single list_fleets() call truncated large
        accounts. With 150 fleets across 2 pages, all 150 must be described via
        two describe_fleet_attributes calls (100 + 50).
        """
        # Standard library
        from unittest.mock import MagicMock, patch

        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        page1 = [f"fleet-{i}" for i in range(100)]
        page2 = [f"fleet-{i}" for i in range(100, 150)]

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.get_paginator.return_value.paginate.return_value = [
                {"FleetIds": page1},
                {"FleetIds": page2},
            ]
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
