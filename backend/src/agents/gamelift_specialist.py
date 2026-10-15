"""
GameLift specialist agent.

Handles GameLift fleet management, scaling, monitoring, and optimization
using boto3 for AWS GameLift operations.
"""

# Standard library
import uuid
from typing import Any

# Third-party packages
import boto3
from strands import tool

# Local modules
from agents.base_specialist import create_specialist_agent
from agents.cfn_template_validation import validate_template_text
from agents.gamelift_projections import (
    GAMELIFT_MAX_PROJECTED_ITEMS,
    error_result,
    log_sanitized_failure,
    project_fleet_capacity,
    project_fleet_utilization,
    project_scaling_policies,
)
from agents.optimized_prompts import get_optimized_gamelift_prompt
from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG, GAMELIFT_KB_ID
from utils.logger import logger

# Re-exported for callers/tests that reason about the projection item bound.
__all__ = [
    "GAMELIFT_MAX_PROJECTED_ITEMS",
    "get_fleet_capacity",
    "get_fleet_utilization",
    "get_scaling_policies",
    "list_gamelift_fleets",
    "validate_cloudformation_template",
]

# ============================================================================
# Boto3 Tools for GameLift Operations
# ============================================================================


def _empty_fleet_response(error: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "FleetAttributes": [],
        "ClassicFleets": [],
        "ContainerFleets": [],
        "ContainerGroupDefinitions": [],
        "FleetCounts": {
            "Classic": 0,
            "Container": 0,
            "Total": 0,
        },
        "Warnings": [],
    }
    if error:
        response["error"] = error
        response["Warnings"].append({"Source": "gamelift", "Message": error})
    return response


def _compact_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _paginate_items(client: Any, operation_name: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in client.get_paginator(operation_name).paginate(**kwargs):
        items.extend(page.get(result_key, []))
    return items


def _extract_definition_version(definition_arn: str | None) -> int | None:
    if not definition_arn:
        return None

    _, _, version = definition_arn.rpartition(":")
    if version.isdigit():
        return int(version)
    return None


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _container_group_summary(definition: dict[str, Any] | None) -> dict[str, Any]:
    if not definition:
        return {}

    return _compact_dict(
        {
            "Name": definition.get("Name"),
            "VersionNumber": definition.get("VersionNumber"),
            "ContainerGroupType": definition.get("ContainerGroupType"),
            "Status": definition.get("Status"),
            "OperatingSystem": definition.get("OperatingSystem"),
            "TotalMemoryLimitMebibytes": definition.get("TotalMemoryLimitMebibytes"),
            "TotalVcpuLimit": definition.get("TotalVcpuLimit"),
        }
    )


def _deployment_status(deployments: list[dict[str, Any]], latest_deployment_id: str | None) -> str | None:
    if not deployments:
        return None

    if latest_deployment_id:
        for deployment in deployments:
            if deployment.get("DeploymentId") == latest_deployment_id:
                return _string_value(deployment.get("DeploymentStatus"))

    return _string_value(deployments[0].get("DeploymentStatus"))


def _summarize_container_fleet(
    fleet: dict[str, Any],
    group_definition: dict[str, Any] | None = None,
    deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    deployment_details = fleet.get("DeploymentDetails", {})
    latest_deployment_id = deployment_details.get("LatestDeploymentId")
    log_configuration = fleet.get("LogConfiguration", {})
    group_definition_version = _extract_definition_version(fleet.get("GameServerContainerGroupDefinitionArn"))

    return _compact_dict(
        {
            "FleetType": "container",
            "Status": fleet.get("Status"),
            "InstanceType": fleet.get("InstanceType"),
            "BillingType": fleet.get("BillingType"),
            "GameServerContainerGroupDefinitionName": fleet.get("GameServerContainerGroupDefinitionName"),
            "GameServerContainerGroupDefinitionVersion": group_definition_version,
            "GameServerContainerGroupsPerInstance": fleet.get("GameServerContainerGroupsPerInstance"),
            "MaximumGameServerContainerGroupsPerInstance": fleet.get("MaximumGameServerContainerGroupsPerInstance"),
            "DeploymentStatus": _deployment_status(deployments or [], latest_deployment_id),
            "LogDestinationType": log_configuration.get("LogDestination"),
            "PlayerGatewayMode": fleet.get("PlayerGatewayMode"),
            "LocationCount": len(fleet.get("LocationAttributes", [])),
            "ContainerGroupDefinition": _container_group_summary(group_definition),
        }
    )


def _list_classic_fleet_attributes(
    client: Any, excluded_fleet_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    # Page through ALL fleets. list_fleets returns at most one page, so an
    # account with many fleets would otherwise be silently truncated.
    warnings: list[dict[str, str]] = []
    fleet_ids: list[str] = []
    for page in client.get_paginator("list_fleets").paginate():
        fleet_ids.extend(page.get("FleetIds", []))

    if excluded_fleet_ids:
        fleet_ids = [fleet_id for fleet_id in fleet_ids if fleet_id not in excluded_fleet_ids]

    if not fleet_ids:
        return [], warnings

    # describe_fleet_attributes accepts at most 100 fleet IDs per call.
    attributes: list[dict[str, Any]] = []
    for i in range(0, len(fleet_ids), 100):
        chunk = fleet_ids[i : i + 100]
        try:
            resp = client.describe_fleet_attributes(FleetIds=chunk)
            attributes.extend(resp.get("FleetAttributes", []))
        except Exception as e:
            logger.warning(f"Failed to describe GameLift classic fleet chunk: {e}")
            warnings.append(
                {
                    "Source": "classic_fleets",
                    "Message": "Some fleet IDs returned by ListFleets were not valid classic fleets and were skipped.",
                }
            )
            for fleet_id in chunk:
                try:
                    resp = client.describe_fleet_attributes(FleetIds=[fleet_id])
                    attributes.extend(resp.get("FleetAttributes", []))
                except Exception as single_error:
                    logger.warning(f"Skipping non-classic GameLift fleet candidate: {single_error}")

    return attributes, warnings


def _list_container_fleet_summaries(
    client: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], set[str]]:
    warnings: list[dict[str, str]] = []
    summaries: list[dict[str, Any]] = []

    container_fleets = _paginate_items(client, "list_container_fleets", "ContainerFleets")
    container_fleet_ids = {fleet["FleetId"] for fleet in container_fleets if isinstance(fleet.get("FleetId"), str)}
    if not container_fleets:
        return [], warnings, container_fleet_ids

    group_definitions_by_key: dict[tuple[str | None, int | None], dict[str, Any]] = {}
    try:
        for definition in _paginate_items(client, "list_container_group_definitions", "ContainerGroupDefinitions"):
            group_definitions_by_key[(definition.get("Name"), definition.get("VersionNumber"))] = definition
            group_definitions_by_key.setdefault((definition.get("Name"), None), definition)
    except Exception as e:
        logger.warning(f"Failed to list GameLift container group definitions: {e}")
        warnings.append({"Source": "container_group_definitions", "Message": str(e)})

    described_group_definitions: dict[tuple[str | None, int | None], dict[str, Any]] = {}
    for listed_fleet in container_fleets:
        fleet = listed_fleet
        fleet_id = listed_fleet.get("FleetId")
        if fleet_id:
            try:
                fleet = client.describe_container_fleet(FleetId=fleet_id).get("ContainerFleet", listed_fleet)
            except Exception as e:
                logger.warning(f"Failed to describe GameLift container fleet: {e}")
                warnings.append({"Source": "container_fleet", "Message": str(e)})

        group_name = fleet.get("GameServerContainerGroupDefinitionName")
        group_version = _extract_definition_version(fleet.get("GameServerContainerGroupDefinitionArn"))
        group_key = (group_name, group_version)
        group_definition = group_definitions_by_key.get(group_key) or group_definitions_by_key.get((group_name, None))

        if group_name and group_key not in described_group_definitions:
            try:
                describe_kwargs: dict[str, Any] = {"Name": group_name}
                if group_version:
                    describe_kwargs["VersionNumber"] = group_version
                group_definition = client.describe_container_group_definition(**describe_kwargs).get(
                    "ContainerGroupDefinition",
                    group_definition,
                )
                described_group_definitions[group_key] = group_definition
            except Exception as e:
                logger.warning(f"Failed to describe GameLift container group definition: {e}")
                warnings.append({"Source": "container_group_definition", "Message": str(e)})
        elif group_key in described_group_definitions:
            group_definition = described_group_definitions[group_key]

        deployments: list[dict[str, Any]] = []
        if fleet_id:
            try:
                deployments = _paginate_items(client, "list_fleet_deployments", "FleetDeployments", FleetId=fleet_id)
            except Exception as e:
                logger.warning(f"Failed to list GameLift fleet deployments: {e}")
                warnings.append({"Source": "fleet_deployments", "Message": str(e)})

        summaries.append(_summarize_container_fleet(fleet, group_definition, deployments))

    return summaries, warnings, container_fleet_ids


@tool
def list_gamelift_fleets() -> dict:  # type: ignore
    """List classic and container GameLift fleets with their attributes."""
    classic_fleets: list[dict[str, Any]] = []
    container_fleets: list[dict[str, Any]] = []
    container_fleet_ids: set[str] = set()
    warnings: list[dict[str, str]] = []

    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
    except Exception as e:
        logger.error(f"Failed to create GameLift client: {e}")
        return _empty_fleet_response(str(e))

    try:
        container_fleets, container_warnings, container_fleet_ids = _list_container_fleet_summaries(client)
        warnings.extend(container_warnings)
    except Exception as e:
        logger.error(f"Failed to list GameLift container fleets: {e}")
        warnings.append({"Source": "container_fleets", "Message": str(e)})

    try:
        classic_fleets, classic_warnings = _list_classic_fleet_attributes(
            client, excluded_fleet_ids=container_fleet_ids
        )
        warnings.extend(classic_warnings)
    except Exception as e:
        logger.error(f"Failed to list GameLift fleets: {e}")
        warnings.append({"Source": "classic_fleets", "Message": str(e)})

    response: dict[str, Any] = {
        "FleetAttributes": classic_fleets,
        "ClassicFleets": classic_fleets,
        "ContainerFleets": container_fleets,
        "FleetCounts": {
            "Classic": len(classic_fleets),
            "Container": len(container_fleets),
            "Total": len(classic_fleets) + len(container_fleets),
        },
        "Warnings": warnings,
    }
    if warnings and not classic_fleets and not container_fleets:
        response["error"] = "; ".join(warning["Message"] for warning in warnings)

    return response


@tool
def get_fleet_utilization(fleet_id: str) -> dict:  # type: ignore
    """Get current utilization metrics for a specific fleet.

    Returns a bounded, code-owned projection with a distinct ``status`` of
    ok / empty / denied / incomplete / truncated. Full ARNs, account IDs, and
    arbitrary provider fields never reach model context; provider exceptions are
    reduced to a typed, sanitized error code.
    """
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        response = client.describe_fleet_utilization(FleetIds=[fleet_id])
    except Exception as e:
        log_sanitized_failure("describe_fleet_utilization", uuid.uuid4().hex, e)
        return error_result("FleetUtilization", e)
    return project_fleet_utilization(response)


@tool
def get_fleet_capacity(fleet_id: str) -> dict:  # type: ignore
    """Get instance capacity information for a specific fleet.

    Returns a bounded, code-owned projection with a distinct ``status`` of
    ok / empty / denied / incomplete / truncated. Full ARNs, account IDs, and
    arbitrary provider fields never reach model context; provider exceptions are
    reduced to a typed, sanitized error code.
    """
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        response = client.describe_fleet_capacity(FleetIds=[fleet_id])
    except Exception as e:
        log_sanitized_failure("describe_fleet_capacity", uuid.uuid4().hex, e)
        return error_result("FleetCapacity", e)
    return project_fleet_capacity(response)


@tool
def get_scaling_policies(fleet_id: str) -> dict:  # type: ignore
    """Get auto-scaling policies for a specific fleet.

    Returns a bounded, code-owned projection with a distinct ``status`` of
    ok / empty / denied / incomplete / truncated. Full ARNs, account IDs, and
    arbitrary provider fields never reach model context; provider exceptions are
    reduced to a typed, sanitized error code.
    """
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
        response = client.describe_scaling_policies(FleetId=fleet_id)
    except Exception as e:
        log_sanitized_failure("describe_scaling_policies", uuid.uuid4().hex, e)
        return error_result("ScalingPolicies", e)
    return project_scaling_policies(response)


@tool
def validate_cloudformation_template(template: str) -> dict:  # type: ignore
    """Check an untrusted CloudFormation draft before you return it. Read-only; creates nothing.

    Pass the complete template text (YAML or JSON). Reports four checks
    separately: safe parsing (syntax), CloudFormation ValidateTemplate, the live
    registry schema for every AWS:: resource (property names with the allowed
    names, required and read-only properties, enum values, value shapes, GetAtt
    attributes), and product policy (deprecated shapes, literal account IDs,
    open ingress, literal IPs). ``status`` is valid, invalid, or incomplete (a
    check could not run; never treat that as valid). Fix every reported problem
    and validate again. Even a valid draft is untrusted and not applied; tell
    the user which checks passed and what ``notValidated`` lists.
    """
    try:
        client = boto3.client("cloudformation", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
    except Exception as e:
        log_sanitized_failure("validate_cloudformation_template", uuid.uuid4().hex, e)
        return validate_template_text(template, _UnavailableCloudFormation())
    return validate_template_text(template, client)


class _UnavailableCloudFormation:
    """Stand-in client when one cannot be built: every provider check is unavailable."""

    def validate_template(self, **_kwargs: Any) -> None:
        raise RuntimeError("cloudformation client unavailable")

    def describe_type(self, **_kwargs: Any) -> None:
        raise RuntimeError("cloudformation client unavailable")


# ============================================================================
# GameLift Agent (using factory pattern)
# ============================================================================

# The ONE runtime tool-registration collection. Model access to GameLift tools
# is defined by exactly this list — it is what create_specialist_agent receives
# and therefore what the agent can call. Tests inject a fake ``agent_factory``
# into :func:`build_gamelift_agent` and assert on the ACTUAL kwargs the builder
# passes to it, so dropping or renaming a tool here is caught as a regression.
GAMELIFT_AGENT_TOOLS = [
    list_gamelift_fleets,
    get_fleet_utilization,
    get_fleet_capacity,
    get_scaling_policies,
    validate_cloudformation_template,
]


def build_gamelift_agent(additional_tools: list | None = None, agent_factory: Any = None):
    """Build the GameLift specialist agent through an injectable factory.

    This is the single construction path for the runtime agent. ``agent_factory``
    defaults to the real :func:`create_specialist_agent`; production always uses
    that default. Tests inject a fake factory and assert on its recorded
    ``call_args`` to observe the EXACT keyword arguments — service name and the
    ``additional_tools`` collection — the builder hands to the factory. There is
    no pre-call alias to drift from the real call: whatever is passed to the
    factory is exactly what these arguments describe.

    Callers that omit ``additional_tools`` get the canonical
    :data:`GAMELIFT_AGENT_TOOLS`.
    """
    factory = create_specialist_agent if agent_factory is None else agent_factory
    tools = GAMELIFT_AGENT_TOOLS if additional_tools is None else additional_tools
    return factory(
        service_name="GameLift",
        emoji="🎮",
        mcp_server_names=None,  # GameLift uses boto3 directly
        kb_id=GAMELIFT_KB_ID,
        prompt_fn=get_optimized_gamelift_prompt,
        fallback_fn=None,  # No fallback needed (boto3 is primary)
        additional_tools=tools,
    )


gamelift_agent = build_gamelift_agent()
