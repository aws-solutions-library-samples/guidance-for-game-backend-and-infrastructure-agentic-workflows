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
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

# Local modules
from agents.base_specialist import create_specialist_agent
from agents.gamelift_projections import (
    ERROR_ACCESS_DENIED,
    ERROR_MALFORMED_RESPONSE,
    ERROR_PROVIDER_ERROR,
    GAMELIFT_MAX_PROJECTED_ITEMS,
    TYPED_ERROR_CODES,
    build_fleet_list_result,
    classify_error,
    error_result,
    log_sanitized_failure,
    log_sanitized_local_fault,
    project_classic_fleet,
    project_container_fleet_summary,
    project_fleet_capacity,
    project_fleet_utilization,
    project_scaling_policies,
)
from agents.optimized_prompts import get_optimized_gamelift_prompt
from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG, GAMELIFT_KB_ID

# Re-exported for callers/tests that reason about the projection item bound.
__all__ = [
    "GAMELIFT_MAX_PROJECTED_ITEMS",
    "get_fleet_capacity",
    "get_fleet_utilization",
    "get_scaling_policies",
    "list_gamelift_fleets",
]

# ============================================================================
# Boto3 Tools for GameLift Operations
# ============================================================================


def _empty_fleet_response(error_code: str | None = None) -> dict[str, Any]:
    """Build a sanitized, bounded empty fleet-listing envelope.

    Used when the GameLift client itself cannot be constructed. Carries the
    distinct ``status`` vocabulary and a typed sanitized ``error`` code; it never
    carries raw provider text, ARNs, or account IDs.
    """
    warnings: list[dict[str, str]] = []
    if error_code:
        warnings.append({"Source": "gamelift", "Code": error_code})
    return build_fleet_list_result(
        classic_rows=[],
        container_rows=[],
        warnings=warnings,
        error_code=error_code,
    )


def _compact_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _classify_local_fault(exc: BaseException) -> str:
    """Classify an exception raised while assembling a container summary.

    A provider ``ClientError`` / ``BotoCoreError`` keeps its typed provider code
    (``throttled`` / ``access_denied`` / ``provider_error`` / ...). Any other
    exception is a LOCAL shape fault (e.g. an unhashable field reaching a dict
    key), not a provider call failure, so it is reported as ``malformed_response``
    rather than being mislabeled ``provider_error``.
    """
    if isinstance(exc, (ClientError, BotoCoreError)):
        return classify_error(exc)
    return ERROR_MALFORMED_RESPONSE


def _paginate_items(
    client: Any,
    operation_name: str,
    result_key: str,
    max_items: int | None = None,
    detect_residual: bool = True,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Page through an operation up to ``max_items``, bounding the pages read.

    Returns ``(items, residual, malformed)``: the collected items (capped at
    ``max_items`` when given), whether more results remained beyond the cap
    (residual pagination), and whether any page shape was malformed. Using the
    paginator's ``PaginationConfig.MaxItems`` keeps the client from draining an
    unbounded number of pages for a large account.

    Shape rules: an ABSENT collection key means empty; a PRESENT but
    wrong-typed collection, or a non-mapping page, is malformed and is NEVER
    iterated. A non-mapping page contributes no items and sets ``malformed``; a
    page whose ``result_key`` is present but not a list likewise contributes no
    items and sets ``malformed``; a page that omits the key is a valid empty
    page. This stops a string collection from being iterated into characters or
    a mapping/number from being treated as rows.

    When ``detect_residual`` is true (the default), the paginator requests one
    extra item so a full page at the cap is distinguishable from a page that
    still had more rows. When false (e.g. a read that only needs the single
    latest item), ``MaxItems`` is used verbatim and no residual is reported.
    """
    paginate_kwargs = dict(kwargs)
    requested = None
    if max_items is not None:
        requested = max_items + 1 if detect_residual else max_items
        paginate_kwargs["PaginationConfig"] = {"MaxItems": requested}
    items: list[dict[str, Any]] = []
    malformed = False
    for page in client.get_paginator(operation_name).paginate(**paginate_kwargs):
        if not isinstance(page, dict):
            malformed = True
            continue
        if result_key not in page:
            continue
        collection = page[result_key]
        if not isinstance(collection, list):
            malformed = True
            continue
        items.extend(collection)
    if max_items is not None and detect_residual and len(items) > max_items:
        return items[:max_items], True, malformed
    if max_items is not None and len(items) > max_items:
        return items[:max_items], False, malformed
    return items, False, malformed


def _extract_definition_version(definition_arn: Any) -> int | None:
    if not isinstance(definition_arn, str) or not definition_arn:
        return None

    _, _, version = definition_arn.rpartition(":")
    if version.isdigit():
        return int(version)
    return None


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _container_group_summary(definition: Any) -> dict[str, Any]:
    if not isinstance(definition, dict):
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


def _deployment_status(deployments: Any, latest_deployment_id: Any) -> str | None:
    if not isinstance(deployments, list) or not deployments:
        return None

    if isinstance(latest_deployment_id, str) and latest_deployment_id:
        for deployment in deployments:
            if isinstance(deployment, dict) and deployment.get("DeploymentId") == latest_deployment_id:
                return _string_value(deployment.get("DeploymentStatus"))

    first = deployments[0]
    if isinstance(first, dict):
        return _string_value(first.get("DeploymentStatus"))
    return None


def _summarize_container_fleet(
    fleet: dict[str, Any],
    group_definition: dict[str, Any] | None = None,
    deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a RAW container-fleet summary dict (field selection only).

    This selects the operational fields but does NOT validate their values; the
    returned dict is passed through :func:`project_container_fleet_summary`,
    which applies field-specific grammar validation and bounds. Keeping
    selection and validation separate avoids duplicating the validators.

    Every provider sub-shape is accessed defensively: a non-mapping
    ``DeploymentDetails`` / ``LogConfiguration``, a non-string group-definition
    name/ARN, or a non-mapping deployment item is tolerated (treated as absent)
    rather than raising, so a malformed container response cannot escape the
    tool or discard already-fetched rows.
    """
    deployment_details = fleet.get("DeploymentDetails")
    if not isinstance(deployment_details, dict):
        deployment_details = {}
    latest_deployment_id = deployment_details.get("LatestDeploymentId")

    log_configuration = fleet.get("LogConfiguration")
    if not isinstance(log_configuration, dict):
        log_configuration = {}

    group_definition_version = _extract_definition_version(fleet.get("GameServerContainerGroupDefinitionArn"))

    location_attributes = fleet.get("LocationAttributes")
    location_count = len(location_attributes) if isinstance(location_attributes, list) and location_attributes else None

    group_name = fleet.get("GameServerContainerGroupDefinitionName")

    return _compact_dict(
        {
            "FleetType": "container",
            "Status": fleet.get("Status"),
            "InstanceType": fleet.get("InstanceType"),
            "BillingType": fleet.get("BillingType"),
            # A non-string name is dropped here (projection would reject it anyway)
            # so it never reaches a dict-key or grammar check as a non-hashable.
            "GameServerContainerGroupDefinitionName": group_name if isinstance(group_name, str) else None,
            "GameServerContainerGroupDefinitionVersion": group_definition_version,
            "GameServerContainerGroupsPerInstance": fleet.get("GameServerContainerGroupsPerInstance"),
            "MaximumGameServerContainerGroupsPerInstance": fleet.get("MaximumGameServerContainerGroupsPerInstance"),
            "DeploymentStatus": _deployment_status(deployments or [], latest_deployment_id),
            "LogDestinationType": log_configuration.get("LogDestination"),
            "PlayerGatewayMode": fleet.get("PlayerGatewayMode"),
            # Only emit a count when the provider actually reported locations; a
            # synthesized 0 would otherwise keep an all-invalid fleet alive as a
            # provider-derived field.
            "LocationCount": location_count,
            "ContainerGroupDefinition": _container_group_summary(group_definition),
        }
    )


def _list_classic_fleet_attributes(
    client: Any, excluded_fleet_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, str]], bool, bool, bool]:
    """Return projected classic fleet rows plus sanitized warnings and flags.

    Returns ``(projected_rows, warnings, truncated, denied, malformed)``:

    * ``projected_rows`` — classic fleets projected through
      :func:`project_classic_fleet` (ARNs, roles, launch paths, metric groups,
      and free-text descriptions dropped);
    * ``warnings`` — code-owned ``{Source, Code}`` entries (never provider text);
    * ``truncated`` — more CLASSIC fleet IDs existed than the item cap allows
      (decided AFTER container-ID exclusion), or the capped paginator stopped at
      the raw ceiling — meaning repeated or interleaved IDs could have filled the
      read before every classic ID was seen, so classic IDs may remain beyond
      what was read even on a final page with no resume token; the returned view
      is therefore bounded;
    * ``denied`` — the provider refused to list fleets;
    * ``malformed`` — the provider returned fleet items some or all of which
      failed projection, a wrong-typed describe response, or a ``list_fleets``
      page whose ``FleetIds`` was present but not a list, so the discarded rows
      must not read as a clean empty result.

    The raw ceiling is ``GAMELIFT_MAX_PROJECTED_ITEMS + len(excluded) + 1`` so
    that, even when the provider interleaves container IDs (which are excluded
    here) with classic IDs, enough raw IDs are read to fill the classic item cap
    AND detect one more. Residual pagination is then decided on the CLASSIC IDs
    that remain after exclusion, on a resume token the capped paginator left
    behind, OR on the raw read reaching the ceiling. Under botocore's paginator
    the ceiling condition subsumes the resume-token signal (the capped iterator
    leaves a token only when it stopped at ``MaxItems``, which also leaves the
    raw read at the ceiling), so the token term is redundant but harmless. A
    complete classic listing of 100 or fewer, read below the ceiling, is never
    mislabeled ``truncated``/``paginated`` merely because container IDs shared
    the stream. The 100-ID chunking for ``describe_fleet_attributes`` is
    preserved on the capped ID set.
    """
    warnings: list[dict[str, str]] = []
    excluded_count = len(excluded_fleet_ids) if excluded_fleet_ids else 0

    # Bound pagination: fetch at most enough raw IDs to fill the CLASSIC item cap
    # even after excluding interleaved container IDs, PLUS one so a full page at
    # the cap is distinguishable from a page that still had more rows. A page
    # whose ``FleetIds`` is present but not a list is a malformed shape and is
    # NEVER iterated; an absent key is a valid empty page.
    raw_ceiling = GAMELIFT_MAX_PROJECTED_ITEMS + excluded_count + 1
    raw_fleet_ids: list[Any] = []
    pages_malformed = False
    more_pages_remained = False
    try:
        paginator = client.get_paginator("list_fleets")
        for page in paginator.paginate(PaginationConfig={"MaxItems": raw_ceiling}):
            if not isinstance(page, dict):
                pages_malformed = True
                more_pages_remained = False
                continue
            if "FleetIds" in page:
                fleet_ids_page = page["FleetIds"]
                if isinstance(fleet_ids_page, list):
                    raw_fleet_ids.extend(fleet_ids_page)
                else:
                    # Present but wrong-typed (null/number/string/mapping): a
                    # malformed shape, never iterated.
                    pages_malformed = True
            # A page carrying a continuation token after the ceiling truncated
            # the stream means the provider still had more fleets to return.
            more_pages_remained = bool(page.get("NextToken"))
    except Exception as exc:  # noqa: BLE001 - sanitized below, never re-raised raw
        log_sanitized_failure("list_fleets", uuid.uuid4().hex, exc)
        denied = classify_error(exc) == ERROR_ACCESS_DENIED
        warnings.append({"Source": "classic_fleets", "Code": classify_error(exc)})
        return [], warnings, False, denied, False

    if pages_malformed:
        warnings.append({"Source": "classic_fleets", "Code": ERROR_MALFORMED_RESPONSE})

    # Keep only string IDs (a non-string entry is a malformed shape, not an ID).
    fleet_ids = [fleet_id for fleet_id in raw_fleet_ids if isinstance(fleet_id, str)]
    bad_id_dropped = pages_malformed or len(fleet_ids) != len(raw_fleet_ids)

    # Exclude container fleets from the classic path. The listed container IDs are
    # excluded explicitly, but the container listing is itself item-capped, so a
    # container fleet beyond that cap would not appear in the exclusion set. A
    # ``containerfleet-`` prefix is an unambiguous container marker, so drop every
    # such ID regardless of the exclusion-set size rather than describing it as a
    # classic fleet.
    if excluded_fleet_ids:
        fleet_ids = [fleet_id for fleet_id in fleet_ids if fleet_id not in excluded_fleet_ids]
    fleet_ids = [fleet_id for fleet_id in fleet_ids if not fleet_id.startswith("containerfleet-")]

    # Decide residual pagination on the CLASSIC IDs (after exclusion), on a resume
    # token the capped paginator left behind, OR when the raw read reached the
    # ceiling. Reaching the ceiling means repeated or interleaved IDs could have
    # filled the read before every classic ID was seen (even with no resume
    # token on a final page), so classic IDs may remain beyond what was read.
    # Excluding container IDs can never hide a residual page, and a complete
    # classic set of <= the cap read below the ceiling is never falsely marked
    # truncated because container IDs shared the stream.
    truncated = (
        len(fleet_ids) > GAMELIFT_MAX_PROJECTED_ITEMS or more_pages_remained or len(raw_fleet_ids) >= raw_ceiling
    )

    # Cap the ID set we describe to the item cap (truncation already decided).
    if len(fleet_ids) > GAMELIFT_MAX_PROJECTED_ITEMS:
        fleet_ids = fleet_ids[:GAMELIFT_MAX_PROJECTED_ITEMS]

    if not fleet_ids:
        return [], warnings, truncated, False, bad_id_dropped

    # describe_fleet_attributes accepts at most 100 fleet IDs per call. The ID
    # list is already capped, so this is a bounded number of chunks.
    projected: list[dict[str, Any]] = []
    described_item_count = 0
    shape_malformed = bad_id_dropped
    for i in range(0, len(fleet_ids), 100):
        chunk = fleet_ids[i : i + 100]
        try:
            resp = client.describe_fleet_attributes(FleetIds=chunk)
        except Exception as exc:  # noqa: BLE001 - sanitized below
            log_sanitized_failure("describe_fleet_attributes", uuid.uuid4().hex, exc)
            warnings.append({"Source": "classic_fleets", "Code": classify_error(exc)})
            # Fall back to per-ID describe so one invalid ID (e.g. a container
            # fleet ID) does not discard the whole chunk. This runs ONLY for an
            # exception, never for a wrong-typed response (see below).
            for fleet_id in chunk:
                try:
                    single_resp = client.describe_fleet_attributes(FleetIds=[fleet_id])
                except Exception as single_exc:  # noqa: BLE001 - sanitized below
                    log_sanitized_failure("describe_fleet_attributes", uuid.uuid4().hex, single_exc)
                    continue
                count, rows, bad = _project_described_fleet_attributes(single_resp)
                described_item_count += count
                projected.extend(rows)
                shape_malformed = shape_malformed or bad
            continue

        # A successful call with a wrong-typed response shape is a malformed
        # response, NOT an authoritative empty and NOT a trigger for the per-ID
        # fallback (an absent key means empty per the service model).
        count, rows, bad = _project_described_fleet_attributes(resp)
        described_item_count += count
        projected.extend(rows)
        shape_malformed = shape_malformed or bad

    # The provider returned fleet items but some or all failed projection, or a
    # response was wrong-typed, or a listed ID was non-string: a mixed collection
    # that retained valid rows while discarding malformed ones (or discarded
    # every row) is partial, not an authoritative inventory.
    malformed = shape_malformed or described_item_count > len(projected)
    return projected, warnings, truncated, False, malformed


def _project_described_fleet_attributes(resp: Any) -> tuple[int, list[dict[str, Any]], bool]:
    """Project a ``describe_fleet_attributes`` response, shape-checking it.

    Returns ``(described_item_count, projected_rows, shape_malformed)``.

    * A non-mapping response, or a present-but-wrong-typed ``FleetAttributes``
      (not a list), is a malformed shape: it contributes no rows and sets
      ``shape_malformed`` without amplifying into a per-ID retry.
    * An ABSENT ``FleetAttributes`` key means empty (the service model gives the
      list a minimum length of 1, so an empty result omits the key); it is not
      malformed.
    """
    if not isinstance(resp, dict):
        return 0, [], True
    if "FleetAttributes" not in resp:
        return 0, [], False
    items = resp.get("FleetAttributes")
    if not isinstance(items, list):
        return 0, [], True
    projected: list[dict[str, Any]] = []
    count = 0
    for item in items:
        count += 1
        row = project_classic_fleet(item)
        if row:
            projected.append(row)
    return count, projected, False


def _list_container_fleet_summaries(
    client: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], set[str], bool, bool, bool, bool]:
    """Return projected container-fleet rows plus warnings, IDs, and flags.

    Returns ``(projected_rows, warnings, container_fleet_ids, truncated, stale,
    denied, malformed)``. The number of container fleets described is bounded by
    the item cap, and each fleet's deployment read is bounded to its single
    latest deployment, so a large account cannot trigger an unbounded N+1
    fan-out. ``stale`` is set when a fleet falls back to
    its list-time summary because ``describe_container_fleet`` failed, returned a
    missing/non-mapping ``ContainerFleet``, or because a group-definition
    describe failed; ``malformed`` is set when some or all listed fleets failed
    projection (non-mapping listed rows are counted as discarded malformed rows).
    A fault reading or indexing the container-group-definition listing (a
    non-mapping page, a wrong-typed collection, a non-mapping row, or an unusable
    name/version) loses only enrichment for container-fleet rows: it adds a
    code-owned ``{Source, Code}`` warning (making the envelope partial) but
    discards no fleet row, so it does NOT set ``malformed``. Every provider
    sub-shape is accessed defensively and a per-fleet safety net converts an
    unexpected exception into a code-owned ``{Source, Code}`` warning on a
    partial envelope rather than letting it escape the tool or discard
    already-fetched rows."""
    warnings: list[dict[str, str]] = []
    summaries: list[dict[str, Any]] = []
    stale = False

    try:
        container_fleets, container_truncated, container_pages_malformed = _paginate_items(
            client, "list_container_fleets", "ContainerFleets", max_items=GAMELIFT_MAX_PROJECTED_ITEMS
        )
    except Exception as exc:  # noqa: BLE001 - sanitized below
        log_sanitized_failure("list_container_fleets", uuid.uuid4().hex, exc)
        denied = classify_error(exc) == ERROR_ACCESS_DENIED
        warnings.append({"Source": "container_fleets", "Code": classify_error(exc)})
        return [], warnings, set(), False, False, denied, False

    if container_pages_malformed:
        # A present but wrong-typed ContainerFleets collection (or a non-mapping
        # page) is a malformed shape, surfaced as a warning and the malformed
        # flag rather than iterated.
        warnings.append({"Source": "container_fleets", "Code": ERROR_MALFORMED_RESPONSE})

    # A non-mapping listed row carries no usable fleet and is a malformed shape.
    dict_fleets = [fleet for fleet in container_fleets if isinstance(fleet, dict)]
    listed_malformed = container_pages_malformed or len(dict_fleets) != len(container_fleets)

    container_fleet_ids = {fleet["FleetId"] for fleet in dict_fleets if isinstance(fleet.get("FleetId"), str)}
    if not dict_fleets:
        return [], warnings, container_fleet_ids, container_truncated, False, False, listed_malformed

    group_definitions_by_key: dict[tuple[Any, int | None], dict[str, Any]] = {}
    try:
        group_defs, _, group_defs_malformed = _paginate_items(
            client,
            "list_container_group_definitions",
            "ContainerGroupDefinitions",
            max_items=GAMELIFT_MAX_PROJECTED_ITEMS,
        )
        if group_defs_malformed:
            # A non-mapping page or a present-but-wrong-typed
            # ContainerGroupDefinitions collection is a malformed shape, surfaced
            # as a code-owned warning on a partial envelope. The group-definition
            # read is enrichment for container-fleet rows; a fault here loses
            # enrichment but discards no fleet row, so it only warns (making the
            # envelope partial) and never sets the discarded-row malformed flag.
            warnings.append({"Source": "container_group_definitions", "Code": ERROR_MALFORMED_RESPONSE})
        for definition in group_defs:
            if not isinstance(definition, dict):
                # A non-mapping group-definition row carries no usable enrichment.
                # Warn (making the envelope partial) and skip it; no fleet row is
                # discarded, so the discarded-row malformed flag stays unset.
                warnings.append({"Source": "container_group_definitions", "Code": ERROR_MALFORMED_RESPONSE})
                continue
            name = definition.get("Name")
            version = definition.get("VersionNumber")
            # The name and version build a dict key, so an unusable value (an
            # unhashable or empty name, or a non-int version) is a local shape
            # fault. Skip the definition with a code-owned malformed warning and
            # keep processing later definitions rather than raising on the key.
            # Losing one definition's enrichment discards no fleet row, so this
            # only warns (making the envelope partial) and does not set the
            # discarded-row malformed flag.
            name_usable = isinstance(name, str) and bool(name)
            version_usable = version is None or (isinstance(version, int) and not isinstance(version, bool))
            if not name_usable or not version_usable:
                warnings.append({"Source": "container_group_definitions", "Code": ERROR_MALFORMED_RESPONSE})
                continue
            group_definitions_by_key[(name, version)] = definition
            group_definitions_by_key.setdefault((name, None), definition)
    except Exception as exc:  # noqa: BLE001 - sanitized below
        log_sanitized_failure("list_container_group_definitions", uuid.uuid4().hex, exc)
        warnings.append({"Source": "container_group_definitions", "Code": classify_error(exc)})

    described_group_definitions: dict[tuple[Any, int | None], dict[str, Any]] = {}
    for listed_fleet in dict_fleets:
        try:
            summary_stale = _summarize_one_container_fleet(
                client,
                listed_fleet,
                group_definitions_by_key,
                described_group_definitions,
                warnings,
                summaries,
            )
            stale = stale or summary_stale
        except Exception as exc:  # noqa: BLE001 - per-fleet safety net
            # An unexpected malformed shape must not escape the tool or drop the
            # rows already collected. A provider ClientError/BotoCoreError keeps
            # its typed provider code; any other exception is a LOCAL shape fault
            # (e.g. an unhashable field) and is recorded as malformed_response
            # with a code-owned local-fault log message, not "provider call
            # failed".
            fault_code = _classify_local_fault(exc)
            if fault_code == ERROR_MALFORMED_RESPONSE:
                log_sanitized_local_fault("summarize_container_fleet", uuid.uuid4().hex, fault_code)
            else:
                log_sanitized_failure("summarize_container_fleet", uuid.uuid4().hex, exc)
            warnings.append({"Source": "container_fleet", "Code": fault_code})
            listed_malformed = True

    malformed = listed_malformed or len(dict_fleets) > len(summaries)
    return summaries, warnings, container_fleet_ids, container_truncated, stale, False, malformed


def _summarize_one_container_fleet(
    client: Any,
    listed_fleet: dict[str, Any],
    group_definitions_by_key: dict[tuple[Any, int | None], dict[str, Any]],
    described_group_definitions: dict[tuple[Any, int | None], dict[str, Any]],
    warnings: list[dict[str, str]],
    summaries: list[dict[str, Any]],
) -> bool:
    """Describe, enrich, and project a single container fleet.

    Appends the projected summary (if any) to ``summaries`` and returns whether
    the row fell back to list-time data (``stale``). Raises only on an
    unexpected shape, which the caller's per-fleet safety net converts into a
    sanitized warning.
    """
    stale = False
    fleet: dict[str, Any] = listed_fleet
    fleet_id = listed_fleet.get("FleetId")
    if isinstance(fleet_id, str):
        try:
            described = client.describe_container_fleet(FleetId=fleet_id)
            container_fleet = described.get("ContainerFleet") if isinstance(described, dict) else None
            if isinstance(container_fleet, dict):
                fleet = container_fleet
            else:
                # Missing / non-mapping ContainerFleet: fall back to list-time
                # data. The row is stale and we record a code-owned warning.
                warnings.append({"Source": "container_fleet", "Code": "stale_list_time_data"})
                stale = True
        except Exception as exc:  # noqa: BLE001 - sanitized below
            log_sanitized_failure("describe_container_fleet", uuid.uuid4().hex, exc)
            warnings.append({"Source": "container_fleet", "Code": classify_error(exc)})
            stale = True

    # Normalize the group-definition name BEFORE it is used as a dict key: a
    # non-empty ``str`` is the only hashable, usable key. A list/dict/other value
    # is a local shape fault, not a key — coerce it to ``None`` so building and
    # looking up ``group_key`` can never raise a ``TypeError`` on an unhashable
    # value (the per-fleet safety net would otherwise mislabel that local fault).
    raw_group_name = fleet.get("GameServerContainerGroupDefinitionName")
    group_name = raw_group_name if isinstance(raw_group_name, str) and raw_group_name else None
    group_version = _extract_definition_version(fleet.get("GameServerContainerGroupDefinitionArn"))
    group_key: tuple[Any, int | None] = (group_name, group_version)
    group_definition = group_definitions_by_key.get(group_key) or group_definitions_by_key.get((group_name, None))

    # Only describe by name when the name is a usable (hashable, string) key.
    if group_name and group_key not in described_group_definitions:
        try:
            describe_kwargs: dict[str, Any] = {"Name": group_name}
            if group_version:
                describe_kwargs["VersionNumber"] = group_version
            described_def = client.describe_container_group_definition(**describe_kwargs)
            described_group = described_def.get("ContainerGroupDefinition") if isinstance(described_def, dict) else None
            if isinstance(described_group, dict):
                group_definition = described_group
                described_group_definitions[group_key] = described_group
            else:
                # A missing / null / non-mapping ContainerGroupDefinition is not
                # a usable describe result: keep the list-time definition, mark
                # the row stale, and record a code-owned warning. Never cache a
                # non-mapping value, which a later fleet sharing the key would
                # otherwise inherit.
                warnings.append({"Source": "container_group_definition", "Code": "stale_list_time_data"})
                stale = True
        except Exception as exc:  # noqa: BLE001 - sanitized below
            log_sanitized_failure("describe_container_group_definition", uuid.uuid4().hex, exc)
            warnings.append({"Source": "container_group_definition", "Code": classify_error(exc)})
            # The row falls back to the list-time group definition (or none).
            stale = True
    elif group_key in described_group_definitions:
        group_definition = described_group_definitions[group_key]

    deployments: list[dict[str, Any]] = []
    if isinstance(fleet_id, str):
        try:
            # Only the latest deployment is used, so read a single item.
            deployments, _, deployments_malformed = _paginate_items(
                client,
                "list_fleet_deployments",
                "FleetDeployments",
                max_items=1,
                detect_residual=False,
                FleetId=fleet_id,
            )
            if deployments_malformed or any(not isinstance(item, dict) for item in deployments):
                # A malformed shape here loses the latest-deployment enrichment
                # but discards no fleet row, so it surfaces a code-owned warning
                # on a partial envelope rather than dropping the signal silently.
                # Two shapes qualify: a non-mapping page or a present-but-wrong-
                # typed FleetDeployments collection (reported by _paginate_items),
                # and a list whose rows are not all mappings — a non-mapping row
                # carries no usable deployment status, mirroring how a non-mapping
                # group-definition row is handled.
                warnings.append({"Source": "fleet_deployments", "Code": ERROR_MALFORMED_RESPONSE})
        except Exception as exc:  # noqa: BLE001 - sanitized below
            log_sanitized_failure("list_fleet_deployments", uuid.uuid4().hex, exc)
            warnings.append({"Source": "fleet_deployments", "Code": classify_error(exc)})

    raw_summary = _summarize_container_fleet(
        fleet, group_definition if isinstance(group_definition, dict) else None, deployments
    )
    projected = project_container_fleet_summary(raw_summary)
    if projected:
        summaries.append(projected)
    return stale


@tool
def list_gamelift_fleets() -> dict:  # type: ignore
    """List classic and container GameLift fleets with bounded, sanitized output.

    Returns separate ``ClassicFleets`` and ``ContainerFleets`` collections and
    ``FleetCounts`` for the model, each row a reviewed, code-owned projection
    (full ARNs, account IDs, role ARNs, launch paths, log paths, metric groups,
    and free-text descriptions never reach model context). The envelope carries a
    distinct ``status`` (ok / empty / denied / incomplete / truncated), the
    independent flags ``truncated`` / ``paginated`` / ``partial`` / ``stale``, a
    typed sanitized ``error`` code, and per-source ``{Source, Code, Count}``
    warnings with no provider text. Collections are item- and aggregate-size
    bounded, and the per-fleet describe / deployment fan-out is bounded.
    """
    try:
        client = boto3.client("gamelift", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
    except Exception as exc:  # noqa: BLE001 - sanitized below
        log_sanitized_failure("create_gamelift_client", uuid.uuid4().hex, exc)
        return _empty_fleet_response(classify_error(exc))

    container_fleets: list[dict[str, Any]] = []
    classic_fleets: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    container_fleet_ids: set[str] = set()
    container_truncated = False
    classic_truncated = False
    stale = False
    container_denied = False
    classic_denied = False
    container_malformed = False
    classic_malformed = False

    (
        container_fleets,
        container_warnings,
        container_fleet_ids,
        container_truncated,
        stale,
        container_denied,
        container_malformed,
    ) = _list_container_fleet_summaries(client)
    warnings.extend(container_warnings)

    (
        classic_fleets,
        classic_warnings,
        classic_truncated,
        classic_denied,
        classic_malformed,
    ) = _list_classic_fleet_attributes(client, excluded_fleet_ids=container_fleet_ids)
    warnings.extend(classic_warnings)

    # The whole listing is ``denied`` only when BOTH listings were refused and
    # neither returned any rows; otherwise a refused sub-listing is a partial
    # view surfaced through warnings/flags.
    listing_denied = container_denied and classic_denied and not container_fleets and not classic_fleets
    malformed = container_malformed or classic_malformed
    error_code: str | None = None
    if listing_denied:
        error_code = ERROR_ACCESS_DENIED
    elif not container_fleets and not classic_fleets and warnings:
        # No rows at all and at least one sub-listing failed (but not a double
        # denial): pin a TYPED error code so the model is not told the account is
        # empty. Derive it from the typed error vocabulary ONLY — a code-owned
        # warning marker such as ``stale_list_time_data`` must never become the
        # envelope error code. Use the shared typed code when every typed warning
        # agrees, else the generic ``provider_error``; when no typed error is
        # present but rows were discarded as malformed, fall back to
        # ``malformed_response``.
        typed_codes = {code for warning in warnings if (code := warning.get("Code")) in TYPED_ERROR_CODES}
        if len(typed_codes) == 1:
            error_code = typed_codes.pop()
        elif typed_codes:
            error_code = ERROR_PROVIDER_ERROR
        elif malformed:
            error_code = ERROR_MALFORMED_RESPONSE
        else:
            error_code = ERROR_PROVIDER_ERROR

    return build_fleet_list_result(
        classic_rows=classic_fleets,
        container_rows=container_fleets,
        warnings=warnings,
        listing_denied=listing_denied,
        classic_truncated=classic_truncated,
        container_truncated=container_truncated,
        stale=stale,
        malformed=malformed,
        error_code=error_code,
    )


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
