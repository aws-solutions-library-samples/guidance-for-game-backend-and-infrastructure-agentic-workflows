#!/usr/bin/env python3
"""Converge an AgentCore runtime on the requested platform version (V1 or V2).

The AgentCore starter toolkit that the deployment uses (``agentcore configure``
and ``agentcore launch``) has no platform-version option, and its update call
does not send ``platformVersion``. An update that omits the field keeps the
runtime's current platform version, so the deployment runs this helper after
its final ``agentcore launch`` to converge the runtime on
``GBAW_AGENTCORE_PLATFORM_VERSION``.

``UpdateAgentRuntime`` is a full replacement (``PUT``) that requires the
artifact and role, so the helper reads the runtime with ``GetAgentRuntime`` and
sends back every configuration field exactly as returned (artifact, role,
network, protocol, authorizer, request-header allowlist, lifecycle, environment
variables, MMDS metadata, description, filesystem, capacity provider, and
additional session-port settings), changing only ``platformVersion``.

Safety properties:

* Idempotent: no update is made when the runtime already reports the requested
  version.
* Fail closed: an unsupported requested value, a read or update error, an
  ``UpdateAgentRuntime`` field this helper does not know how to preserve (for
  example after a botocore upgrade adds one), a preserved field that is missing
  or different after the update, a failed or unexpected status, or a timeout
  exits non-zero so the calling deployment stops.
* Content-free output: only field names, versions, and statuses are printed,
  never configuration values such as environment variables.

``--wait-only`` waits for the runtime to leave ``CREATING``/``UPDATING`` without
changing it. The deployment runs it before an ``agentcore launch`` that updates
an existing runtime, because on platform version V2 a create or update takes
several minutes and an update issued before a terminal status fails with
``ConflictException``.

Rolling back is the same operation with ``--platform-version V1``. It also
works when the last update failed (for example a V2 startup that did not report
healthy in time): a failed runtime that reports a different version than the
one requested is updated as a recovery, while a failed runtime that already
reports the requested version stops the deployment.
"""

from __future__ import annotations

# Standard library
import argparse
import copy
import sys
import time
from typing import Any, Callable, Mapping

SUPPORTED_PLATFORM_VERSIONS = ("V1", "V2")
PLATFORM_VERSION_ENV = "GBAW_AGENTCORE_PLATFORM_VERSION"

# Every UpdateAgentRuntime request member that carries runtime configuration.
# Each is copied unchanged from GetAgentRuntime into the update request.
PRESERVED_FIELDS = (
    "agentRuntimeArtifact",
    "roleArn",
    "networkConfiguration",
    "protocolConfiguration",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "lifecycleConfiguration",
    "environmentVariables",
    "metadataConfiguration",
    "description",
    "filesystemConfigurations",
    "capacityProviderConfiguration",
    # Additional ports reachable through the runtime's session URL. Copied as
    # read, like every field here: the helper never opens or closes a port.
    "allowedPorts",
)
REQUIRED_FIELDS = ("agentRuntimeArtifact", "roleArn")

# UpdateAgentRuntime request members that are not copied runtime configuration:
# the path identifier, the idempotency token botocore fills in, and the field
# this helper sets.
NON_CONFIGURATION_MEMBERS = frozenset({"agentRuntimeId", "clientToken", "platformVersion"})

IN_PROGRESS_STATUSES = frozenset({"CREATING", "UPDATING"})
FAILED_STATUSES = frozenset({"CREATE_FAILED", "UPDATE_FAILED"})

DEFAULT_TIMEOUT_SECONDS = 1200
DEFAULT_POLL_SECONDS = 10


class PlatformVersionError(RuntimeError):
    """A condition that must stop the deployment."""


def validate_platform_version(value: str) -> str:
    """Return ``value`` if it is a supported platform version, else raise."""
    if value not in SUPPORTED_PLATFORM_VERSIONS:
        raise PlatformVersionError(f"{PLATFORM_VERSION_ENV}={value!r} is not supported; use V1 or V2.")
    return value


def check_service_model(client: Any) -> None:
    """Fail closed unless this helper can preserve every updatable field.

    The update request must accept exactly ``PRESERVED_FIELDS`` plus the
    non-configuration members, and the read must return every preserved field
    and the platform version. A field the helper does not copy would otherwise
    be reset by the replacement update; a field the read does not return could
    not be copied.
    """
    service_model = getattr(getattr(client, "meta", None), "service_model", None)
    if service_model is None:
        raise PlatformVersionError("The AgentCore control-plane client exposes no service model to check.")
    update_members = set(service_model.operation_model("UpdateAgentRuntime").input_shape.members)
    read_members = set(service_model.operation_model("GetAgentRuntime").output_shape.members)

    unknown = sorted(update_members - NON_CONFIGURATION_MEMBERS - set(PRESERVED_FIELDS))
    if unknown:
        raise PlatformVersionError(
            "UpdateAgentRuntime accepts configuration this helper does not preserve: "
            f"{', '.join(unknown)}. Refusing to update; add the field(s) to PRESERVED_FIELDS after review."
        )
    not_updatable = sorted((set(PRESERVED_FIELDS) | {"platformVersion"}) - update_members)
    not_readable = sorted((set(PRESERVED_FIELDS) | {"platformVersion"}) - read_members)
    if not_updatable or not_readable:
        raise PlatformVersionError(
            "The installed botocore cannot read and update every runtime configuration field "
            f"(not updatable: {', '.join(not_updatable) or 'none'}; not readable: {', '.join(not_readable) or 'none'}). "
            "Refusing to update; run with the backend's locked dependencies (uv sync)."
        )


def build_update_request(runtime: Mapping[str, Any], runtime_id: str, platform_version: str) -> dict[str, Any]:
    """Build an UpdateAgentRuntime request that changes only the platform version."""
    missing = [name for name in REQUIRED_FIELDS if not runtime.get(name)]
    if missing:
        raise PlatformVersionError(f"GetAgentRuntime returned no {', '.join(missing)}; refusing to update.")
    request: dict[str, Any] = {"agentRuntimeId": runtime_id, "platformVersion": platform_version}
    # An absent (or empty-string) value means the setting is not configured, so
    # it is left out, exactly as the read reported it.
    request.update(copy.deepcopy(configured_fields(runtime)))
    return request


def _contains(expected: Any, actual: Any) -> bool:
    """Return True if ``actual`` still holds everything in ``expected``.

    Mappings may gain keys (a service-populated default) but must keep every
    expected key with an equal value; lists and scalars must be equal.
    """
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(key in actual and _contains(value, actual[key]) for key, value in expected.items())
    return bool(expected == actual)


def configured_fields(runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Return the preserved fields the runtime reports as configured (non-empty)."""
    return {name: runtime[name] for name in PRESERVED_FIELDS if runtime.get(name) not in (None, "")}


def preserved_field_drift(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Return the fields configured before the update that are missing or different after it.

    The comparison is against the runtime as read before the update, not the
    request, so a field the request failed to carry is caught too.
    """
    expected = configured_fields(before)
    return [name for name in PRESERVED_FIELDS if name in expected and not _contains(expected[name], after.get(name))]


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str(response.get("Error", {}).get("Code", "") or "")
    return ""


class _Waiter:
    """Bounded polling with an injectable clock and sleep for tests."""

    def __init__(self, timeout_seconds: float, poll_seconds: float, clock: Callable[[], float], sleep) -> None:
        self._deadline = clock() + timeout_seconds
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep

    def pause_or_fail(self, what: str, status: str) -> None:
        if self._clock() >= self._deadline:
            raise PlatformVersionError(
                f"{what} did not settle within {self._timeout_seconds:g}s (last status {status}). "
                "Re-run the deployment once it settles; this step is idempotent."
            )
        self._sleep(self._poll_seconds)


def wait_for_runtime(
    client: Any,
    runtime_id: str,
    waiter: _Waiter,
    *,
    expected_version: str | None = None,
    accept_failed: bool = False,
) -> dict[str, Any]:
    """Poll GetAgentRuntime until the runtime is READY (and at ``expected_version``).

    With ``accept_failed`` a ``CREATE_FAILED``/``UPDATE_FAILED`` status is also
    returned, because a later update may repair it; otherwise it is an error.
    """
    while True:
        runtime = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = str(runtime.get("status") or "UNKNOWN")
        version = runtime.get("agentRuntimeVersion")
        if status in IN_PROGRESS_STATUSES or (expected_version is not None and version != expected_version):
            if status in FAILED_STATUSES:
                reason = runtime.get("failureReason")
                raise PlatformVersionError(f"Runtime status is {status}" + (f": {reason}" if reason else "."))
            waiter.pause_or_fail("The AgentCore runtime", status)
            continue
        if status == "READY" or (accept_failed and status in FAILED_STATUSES):
            return runtime
        reason = runtime.get("failureReason")
        raise PlatformVersionError(f"Runtime status is {status}" + (f": {reason}" if reason else "."))


def wait_for_default_endpoint(client: Any, runtime_id: str, version: str, waiter: _Waiter) -> None:
    """Poll the DEFAULT endpoint until it is READY and serving ``version``."""
    while True:
        try:
            endpoint = client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
        except Exception as exc:
            if _error_code(exc) != "ResourceNotFoundException":
                raise
            waiter.pause_or_fail("The DEFAULT endpoint", "NOT_FOUND")
            continue
        status = str(endpoint.get("status") or "UNKNOWN")
        if status in FAILED_STATUSES:
            reason = endpoint.get("failureReason")
            raise PlatformVersionError(f"DEFAULT endpoint status is {status}" + (f": {reason}" if reason else "."))
        if status == "READY" and endpoint.get("liveVersion") == version:
            return
        if status not in IN_PROGRESS_STATUSES and status != "READY":
            raise PlatformVersionError(f"DEFAULT endpoint status is {status}.")
        waiter.pause_or_fail("The DEFAULT endpoint", status)


def apply_platform_version(
    client: Any,
    runtime_id: str,
    platform_version: str,
    waiter: _Waiter,
    out: Callable[[str], None] = print,
) -> str:
    """Converge the runtime on ``platform_version``; return ``"unchanged"`` or ``"updated"``.

    A runtime whose last create or update failed is never reported as converged.
    If it already reports the requested version, the failure stops the
    deployment; if it reports another version, the update is applied as a
    recovery, so a failed V2 update can be rolled back to V1 (and the reverse).
    """
    validate_platform_version(platform_version)
    check_service_model(client)

    before = wait_for_runtime(client, runtime_id, waiter, accept_failed=True)
    status = str(before.get("status"))
    current = before.get("platformVersion")
    version = str(before.get("agentRuntimeVersion"))
    if status != "READY":
        reason = before.get("failureReason")
        detail = f"Runtime status is {status}" + (f": {reason}" if reason else ".")
        if current == platform_version:
            raise PlatformVersionError(detail)
        out(f"⚠️  {detail} Applying platform version {platform_version} as a recovery update.")
    elif current == platform_version:
        out(f"✅ AgentCore runtime already on platform version {platform_version} (runtime version {version})")
        wait_for_default_endpoint(client, runtime_id, version, waiter)
        return "unchanged"

    request = build_update_request(before, runtime_id, platform_version)
    preserved = [name for name in PRESERVED_FIELDS if name in request]
    out(
        f"🔄 Updating AgentCore runtime platform version {current or 'unreported'} -> {platform_version}; "
        f"preserving {len(preserved)} configuration field(s): {', '.join(preserved)}"
    )
    response = client.update_agent_runtime(**request)
    new_version = response.get("agentRuntimeVersion")
    if not new_version:
        raise PlatformVersionError("UpdateAgentRuntime returned no runtime version.")
    new_version = str(new_version)
    out(f"⏳ Waiting for runtime version {new_version} (platform version {platform_version}) to become READY...")

    after = wait_for_runtime(client, runtime_id, waiter, expected_version=new_version)
    if after.get("platformVersion") != platform_version:
        raise PlatformVersionError(
            f"Runtime reports platform version {after.get('platformVersion') or 'unreported'} after the update, "
            f"not {platform_version}."
        )
    drift = preserved_field_drift(before, after)
    if drift:
        raise PlatformVersionError(
            "Runtime configuration was not preserved by the platform update: "
            f"{', '.join(drift)}. Values are not printed; inspect the runtime before retrying."
        )
    added = [name for name in PRESERVED_FIELDS if name not in request and after.get(name) not in (None, "")]
    if added:
        out(f"ℹ️  The service now reports default values for: {', '.join(added)}")

    wait_for_default_endpoint(client, runtime_id, new_version, waiter)
    out(
        f"✅ AgentCore runtime on platform version {platform_version} "
        f"(runtime version {version} -> {new_version}); the DEFAULT endpoint serves it"
    )
    return "updated"


def wait_only(client: Any, runtime_id: str, waiter: _Waiter, out: Callable[[str], None] = print) -> str:
    """Wait for the runtime to reach a terminal status without changing it; return the status."""
    runtime = wait_for_runtime(client, runtime_id, waiter, accept_failed=True)
    status = str(runtime.get("status"))
    out(
        f"✅ AgentCore runtime status {status} (runtime version {runtime.get('agentRuntimeVersion')}, "
        f"platform version {runtime.get('platformVersion') or 'unreported'})"
    )
    return status


def build_client(region: str) -> Any:
    """Create the AgentCore control-plane client (imported lazily for tests)."""
    # Third-party packages
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
        config=Config(connect_timeout=10, read_timeout=60, retries={"mode": "standard", "max_attempts": 5}),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-id", required=True, help="AgentCore runtime ID")
    parser.add_argument("--region", required=True, help="AWS Region of the runtime")
    parser.add_argument("--platform-version", help="Requested platform version: V1 or V2")
    parser.add_argument(
        "--wait-only", action="store_true", help="Only wait for a terminal runtime status; never update"
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    if not args.wait_only and args.platform_version is None:
        parser.error("--platform-version is required unless --wait-only is given")
    if args.timeout_seconds <= 0 or args.poll_seconds < 0:
        parser.error("--timeout-seconds must be positive and --poll-seconds non-negative")
    return args


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], Any] = build_client,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    args = _parse_args(argv)
    try:
        if not args.wait_only:
            validate_platform_version(args.platform_version)
        client = client_factory(args.region)
        waiter = _Waiter(args.timeout_seconds, args.poll_seconds, clock, sleep)
        if args.wait_only:
            wait_only(client, args.runtime_id, waiter, out)
        else:
            apply_platform_version(client, args.runtime_id, args.platform_version, waiter, out)
        return 0
    except PlatformVersionError as exc:
        print(f"❌ AgentCore platform version: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # boto3/botocore errors and anything unexpected stop the deployment.
        code = _error_code(exc)
        print(
            f"❌ AgentCore platform version: {type(exc).__name__}{f' ({code})' if code else ''}: {exc}", file=sys.stderr
        )
        if code == "ValidationException" and getattr(args, "platform_version", None) == "V2":
            print(
                "   Platform version V2 is available only in some AWS Regions; see the AgentCore Runtime "
                "platform versions documentation.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
