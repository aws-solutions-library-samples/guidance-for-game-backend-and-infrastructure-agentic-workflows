"""Trusted E1-backed capacity-state port for the E2 prepare layer (issue #414).

:class:`E1ObservationCapacityStatePort` adapts the read-only E1 observation
status into the :class:`~operations.advice.CapacityStatePort` the E2
``AdviceService`` depends on. It is the sole bridge that lets prepare read the
*current* fleet capacity: the untrusted prepare body supplies only the
``observation_id`` it believes it is acting on (plus the capacity proposal), and
this port resolves that id against the durable E1 store under the **verified
workspace** — never trusting the body for identity, capacity, or freshness.

Trust boundary (fails closed to ``None``; it never raises and never fabricates):

* **Verified workspace only.** The observation is loaded through the injected
  E1 status loader scoped to ``requester.workspace_id``; an observation owned by
  another workspace is invisible.
* **Successful + fresh only.** A non-``succeeded`` observation, or one at/after
  its own ``expires_at`` on the trusted clock, yields ``None``.
* **Exact target.** The observation's ``target.fleet_id`` must equal the
  requested fleet and its ``results.capacity`` must contain the requested
  location; otherwise ``None``.
* **Integrity-bound.** The observation must carry the E1 store's recorded
  ``observation_hash``; a hash-less or malformed record yields ``None``.

The returned :class:`~operations.advice.CurrentCapacity` carries the E1
observation revision's own ``observed_at``/``expires_at`` as the deterministic
time anchor for advice and the prepared operation. The port performs no provider
read, holds no credential, and issues no write.
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

# Local modules
from operations.advice import CapacityValues, CurrentCapacity
from operations.identity import VerifiedPrincipal
from operations.observation import ObservationStatus, ObservationStatusView


class ObservationStatusLoader(Protocol):
    """The narrow slice of the E1 store this port reads (workspace-scoped)."""

    def load_status(self, *, operation_id: str, workspace_id: str) -> ObservationStatus | None: ...


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _int_field(mapping: object, name: str) -> int | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class E1ObservationCapacityStatePort:
    """Resolve current fleet capacity from a trusted E1 observation revision."""

    def __init__(
        self,
        *,
        status_loader: ObservationStatusLoader,
        observation_id: str,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        self._status_loader = status_loader
        self._observation_id = observation_id
        self._clock = clock

    def load_current_capacity(
        self, *, requester: VerifiedPrincipal, fleet_id: str, location: str
    ) -> CurrentCapacity | None:
        """Return trusted current capacity for one fleet/location, or ``None``."""
        status = self._status_loader.load_status(operation_id=self._observation_id, workspace_id=requester.workspace_id)
        if status is None or status.state is not ObservationStatusView.SUCCEEDED:
            return None
        observation = status.observation
        observation_hash = status.observation_hash
        if not isinstance(observation, dict) or not isinstance(observation_hash, str) or not observation_hash:
            return None

        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id:
            return None

        target = observation.get("target")
        if not isinstance(target, dict) or target.get("fleet_id") != fleet_id:
            return None

        observed_at = _parse_timestamp(observation.get("observed_at"))
        expires_at = _parse_timestamp(observation.get("expires_at"))
        if observed_at is None or expires_at is None or expires_at <= observed_at:
            return None

        # The live clock only decides freshness of the trusted revision; it never
        # contributes to the emitted advice/prepared bytes downstream.
        now = self._clock().astimezone(timezone.utc)
        if now >= expires_at:
            return None

        capacity = self._select_capacity(observation, location)
        if capacity is None:
            return None

        try:
            return CurrentCapacity(
                observation_id=observation_id,
                observation_hash=observation_hash,
                capacity=capacity,
                observed_at=observed_at,
                expires_at=expires_at,
            )
        except ValueError:
            return None

    @staticmethod
    def _select_capacity(observation: dict[str, Any], location: str) -> CapacityValues | None:
        results = observation.get("results")
        if not isinstance(results, dict):
            return None
        entries = results.get("capacity")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("location") != location:
                continue
            desired = _int_field(entry, "desired")
            minimum = _int_field(entry, "minimum")
            maximum = _int_field(entry, "maximum")
            if desired is None or minimum is None or maximum is None:
                return None
            return CapacityValues(desired=desired, minimum=minimum, maximum=maximum)
        return None
