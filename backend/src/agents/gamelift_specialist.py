"""
GameLift specialist agent.

Handles GameLift fleet management, scaling, monitoring, and optimization
using boto3 for AWS GameLift operations.
"""

# Third-party packages
import boto3
from strands import tool

# Local modules
from agents.base_specialist import create_specialist_agent
from agents.optimized_prompts import get_optimized_gamelift_prompt
from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG, GAMELIFT_KB_ID
from utils.logger import logger

# ============================================================================
# Boto3 Tools for GameLift Operations
# ============================================================================


@tool
def list_gamelift_fleets() -> dict:  # type: ignore
    """List all GameLift fleets with their attributes."""
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)

        # Page through ALL fleets — list_fleets returns at most one page, so an
        # account with many fleets would otherwise be silently truncated.
        fleet_ids: list[str] = []
        for page in client.get_paginator("list_fleets").paginate():
            fleet_ids.extend(page.get("FleetIds", []))

        if not fleet_ids:
            return {"FleetAttributes": []}

        # describe_fleet_attributes accepts at most 100 fleet IDs per call.
        attributes: list = []
        for i in range(0, len(fleet_ids), 100):
            chunk = fleet_ids[i : i + 100]
            resp = client.describe_fleet_attributes(FleetIds=chunk)
            attributes.extend(resp.get("FleetAttributes", []))

        return {"FleetAttributes": attributes}
    except Exception as e:
        logger.error(f"Failed to list GameLift fleets: {e}")
        return {"error": str(e), "FleetAttributes": []}


@tool
def get_fleet_utilization(fleet_id: str) -> dict:  # type: ignore
    """Get current utilization metrics for a specific fleet."""
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        return client.describe_fleet_utilization(FleetIds=[fleet_id])  # type: ignore
    except Exception as e:
        logger.error(f"Failed to get fleet utilization for {fleet_id}: {e}")
        return {"error": str(e), "FleetUtilization": []}


@tool
def get_fleet_capacity(fleet_id: str) -> dict:  # type: ignore
    """Get instance capacity information for a specific fleet."""
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        return client.describe_fleet_capacity(FleetIds=[fleet_id])  # type: ignore
    except Exception as e:
        logger.error(f"Failed to get fleet capacity for {fleet_id}: {e}")
        return {"error": str(e), "FleetCapacity": []}


@tool
def get_scaling_policies(fleet_id: str) -> dict:  # type: ignore
    """Get auto-scaling policies for a specific fleet."""
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        return client.describe_scaling_policies(FleetId=fleet_id)  # type: ignore
    except Exception as e:
        logger.error(f"Failed to get scaling policies for {fleet_id}: {e}")
        return {"error": str(e), "ScalingPolicies": []}


# ============================================================================
# GameLift Agent (using factory pattern)
# ============================================================================

gamelift_agent = create_specialist_agent(
    service_name="GameLift",
    emoji="🎮",
    mcp_server_names=None,  # GameLift uses boto3 directly
    kb_id=GAMELIFT_KB_ID,
    prompt_fn=get_optimized_gamelift_prompt,
    fallback_fn=None,  # No fallback needed (boto3 is primary)
    additional_tools=[list_gamelift_fleets, get_fleet_utilization, get_fleet_capacity, get_scaling_policies],
)
