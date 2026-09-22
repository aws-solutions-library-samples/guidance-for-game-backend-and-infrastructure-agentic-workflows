"""Normalized GameLift execution adapter for the E3 execute phase (#415).

This adapter owns the SDK and the least-privilege credentials for the single
provider *write* this milestone permits — GameLift ``UpdateFleetCapacity`` —
plus the read used immediately before and after the write,
``DescribeFleetCapacity``. It exposes exactly two public methods:

* ``describe_capacity`` — a single bounded ``describe_fleet_capacity`` read,
  normalized to the bounded ``{desired, minimum, maximum}`` domain shape for the
  requested fleet+location. Every provider-specific field — the fleet ARN,
  account id, timestamps, the raw response — is dropped.
* ``update_capacity`` — a single bounded ``update_fleet_capacity`` write with
  exactly the desired/min/max for the fleet+location. There is no other write
  method, no generic ``call``/``invoke``, no shell, and no credential surface.

Result classification is the crux of safe execution:

* A deterministic provider error (a ``ClientError``) is a **clear rejection**
  (:class:`ProviderWriteRejected`): the write did not take effect. It is
  classified into a bounded, public-safe ``error_code`` and the raw provider
  message is never surfaced.
* A connect/read timeout or other lost-response condition is **inconclusive**
  (:class:`ProviderWriteInconclusive`): the write may or may not have landed.
  The caller MUST ``describe_capacity`` before any retry and MUST NOT blind
  retry; if the post-timeout Describe cannot conclusively confirm the outcome,
  the caller records ``HUMAN_RECONCILIATION_REQUIRED``.
"""

from __future__ import annotations

# Standard library
from typing import Any, Protocol

_MIN_INSTANCES = 0
_MAX_INSTANCES = 1_000_000


class ExecutionGameLiftClient(Protocol):
    """The narrow slice of the boto3 GameLift client this adapter uses."""

    def describe_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]: ...


class ProviderWriteRejected(RuntimeError):
    """The provider deterministically rejected the write; it did not take effect."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__("provider rejected the capacity write")


class ProviderWriteInconclusive(RuntimeError):
    """The write result is unknown (timeout / lost response); Describe before retry."""

    def __init__(self) -> None:
        super().__init__("provider capacity write result is inconclusive")


def _bounded_instances(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer instance count")
    if not (_MIN_INSTANCES <= value <= _MAX_INSTANCES):
        raise ValueError(f"{field_name} is out of the bounded instance range")
    return value


def _is_timeout(exc: BaseException) -> bool:
    """Return whether an exception is a lost-response / timeout condition."""
    # Third-party packages
    try:
        # Third-party packages
        from botocore.exceptions import (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
    except Exception:  # noqa: BLE001 - botocore always present in the runtime
        return False
    return isinstance(
        exc,
        (ReadTimeoutError, ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError),
    )


def _is_client_error(exc: BaseException) -> bool:
    # Third-party packages
    try:
        # Third-party packages
        from botocore.exceptions import ClientError
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, ClientError)


class GameLiftExecutionAdapter:
    """Read + single-write GameLift adapter with safe result classification."""

    def __init__(self, client: ExecutionGameLiftClient) -> None:
        self._client = client

    def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
        """Return the bounded, normalized current capacity for one fleet+location.

        A timeout here is raised as :class:`ProviderWriteInconclusive` so the
        executor treats an unreadable current state as inconclusive, never as a
        confirmed value.
        """
        try:
            response = self._client.describe_fleet_capacity(FleetIds=[fleet_id])
        except BaseException as exc:  # noqa: BLE001 - classify, never leak
            if _is_timeout(exc):
                raise ProviderWriteInconclusive() from exc
            if _is_client_error(exc):
                raise ProviderWriteRejected("PROVIDER_ERROR") from exc
            raise ProviderWriteRejected("PROVIDER_ERROR") from exc

        capacities = response.get("FleetCapacity") if isinstance(response, dict) else None
        if not isinstance(capacities, list) or not capacities:
            raise ValueError("no fleet capacity returned")
        for entry in capacities:
            if not isinstance(entry, dict):
                raise ValueError("malformed fleet capacity record")
            entry_location = entry.get("Location")
            resolved = entry_location if isinstance(entry_location, str) and entry_location else "home"
            if resolved != location:
                continue
            instances = entry.get("InstanceCounts")
            if not isinstance(instances, dict):
                raise ValueError("malformed fleet capacity counts")
            return {
                "desired": _bounded_instances(instances.get("DESIRED"), "desired"),
                "minimum": _bounded_instances(instances.get("MINIMUM"), "minimum"),
                "maximum": _bounded_instances(instances.get("MAXIMUM"), "maximum"),
            }
        raise ValueError("requested location not present in fleet capacity")

    def update_capacity(
        self,
        *,
        fleet_id: str,
        location: str,
        desired: int,
        minimum: int,
        maximum: int,
    ) -> None:
        """Issue exactly one bounded UpdateFleetCapacity write.

        A ``ClientError`` is a clear rejection (:class:`ProviderWriteRejected`);
        a timeout / lost response is :class:`ProviderWriteInconclusive`. This
        method never retries — the caller owns the Describe-before-retry policy.
        """
        desired = _bounded_instances(desired, "desired")
        minimum = _bounded_instances(minimum, "minimum")
        maximum = _bounded_instances(maximum, "maximum")
        if not (minimum <= desired <= maximum):
            raise ValueError("capacity write is not within [minimum, maximum]")

        try:
            self._client.update_fleet_capacity(
                FleetId=fleet_id,
                Location=location,
                DesiredInstances=desired,
                MinSize=minimum,
                MaxSize=maximum,
            )
        except BaseException as exc:  # noqa: BLE001 - classify, never leak
            if _is_timeout(exc):
                # The write may have landed; the outcome is unknown.
                raise ProviderWriteInconclusive() from exc
            if _is_client_error(exc):
                raise ProviderWriteRejected("PROVIDER_ERROR") from exc
            # Any other unexpected error is treated as inconclusive: we cannot
            # prove the write did not land, so fail closed to reconciliation.
            raise ProviderWriteInconclusive() from exc
