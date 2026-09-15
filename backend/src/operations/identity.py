"""Trusted, protocol-neutral identity values for operations boundaries."""

from __future__ import annotations

# Standard library
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a valid operations identifier")
    return value


def _require_text(value: object, field_name: str, *, max_length: int = 2048) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{field_name} must be a nonempty normalized string")
    return value


def _freeze_claims(values: Iterable[str], field_name: str) -> frozenset[str]:
    if isinstance(values, str):
        raise ValueError(f"{field_name} must be a collection of strings")
    frozen = frozenset(values)
    if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 256 for value in frozen):
        raise ValueError(f"{field_name} contains an invalid value")
    return frozen


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class IdentityBoundaryReason(str, Enum):
    """Stable internal reasons for rejecting an identity at the boundary."""

    CREDENTIAL_EXPIRED = "credential_expired"
    TENANT_MISMATCH = "tenant_mismatch"
    WORKSPACE_MISMATCH = "workspace_mismatch"
    CLIENT_NOT_TRUSTED = "client_not_trusted"
    AUDIENCE_NOT_TRUSTED = "audience_not_trusted"
    REQUESTER_IDENTITY_INVALID = "requester_identity_invalid"


class IdentityBoundaryError(ValueError):
    """A verified principal or stored requester is outside the configured boundary."""

    def __init__(self, reason: IdentityBoundaryReason) -> None:
        self.reason = reason
        super().__init__("identity does not satisfy the configured operations boundary")


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedPrincipal:
    """Identity produced by an adapter after cryptographic token verification.

    Adapters must verify issuer, signature, token use, expiration, client, and
    audience before constructing this value. It is a trusted capability, not a
    request-deserializable data-transfer object. Tokens and presentation claims
    do not enter the operations application layer.
    """

    subject_id: str
    client_id: str
    audience: str
    tenant_id: str
    workspace_id: str
    expires_at: datetime
    groups: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_id", _require_identifier(self.subject_id, "subject_id"))
        object.__setattr__(self, "client_id", _require_identifier(self.client_id, "client_id"))
        object.__setattr__(self, "audience", _require_text(self.audience, "audience"))
        object.__setattr__(self, "tenant_id", _require_identifier(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "workspace_id", _require_identifier(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "expires_at", _aware_utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "groups", _freeze_claims(self.groups, "groups"))
        object.__setattr__(self, "scopes", _freeze_claims(self.scopes, "scopes"))

    def contract_identity(self) -> dict[str, str]:
        """Return the bounded identity fields permitted in operation contracts."""
        return {
            "subject_id": self.subject_id,
            "client_id": self.client_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalIdentityBoundary:
    """Trusted deployment binding for operation requesters and approvers."""

    tenant_id: str
    workspace_id: str
    requester_client_ids: frozenset[str]
    approver_client_ids: frozenset[str]
    trusted_audiences: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _require_identifier(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "workspace_id", _require_identifier(self.workspace_id, "workspace_id"))
        object.__setattr__(
            self,
            "requester_client_ids",
            self._required_identifiers(self.requester_client_ids, "requester_client_ids"),
        )
        object.__setattr__(
            self,
            "approver_client_ids",
            self._required_identifiers(self.approver_client_ids, "approver_client_ids"),
        )
        audiences = _freeze_claims(self.trusted_audiences, "trusted_audiences")
        if not audiences:
            raise ValueError("trusted_audiences must not be empty")
        object.__setattr__(self, "trusted_audiences", audiences)

    @staticmethod
    def _required_identifiers(values: Iterable[str], field_name: str) -> frozenset[str]:
        frozen = _freeze_claims(values, field_name)
        if not frozen:
            raise ValueError(f"{field_name} must not be empty")
        for value in frozen:
            _require_identifier(value, field_name)
        return frozen

    def bind_requester(self, principal: VerifiedPrincipal, *, now: datetime) -> dict[str, str]:
        """Validate a fresh requester and return its immutable contract identity."""
        self._validate_verified(principal, now=now, trusted_clients=self.requester_client_ids)
        return principal.contract_identity()

    def bind_approver(self, principal: VerifiedPrincipal, *, now: datetime) -> dict[str, str]:
        """Validate a fresh approver and return its immutable contract identity."""
        self._validate_verified(principal, now=now, trusted_clients=self.approver_client_ids)
        return principal.contract_identity()

    def validate_stored_requester(self, requester: Mapping[str, object]) -> None:
        """Verify that a stored requester remains inside this deployment boundary."""
        required = {"subject_id", "client_id", "tenant_id", "workspace_id"}
        if set(requester) != required:
            raise IdentityBoundaryError(IdentityBoundaryReason.REQUESTER_IDENTITY_INVALID)
        try:
            _require_identifier(requester["subject_id"], "subject_id")
            client_id = _require_identifier(requester["client_id"], "client_id")
            tenant_id = _require_identifier(requester["tenant_id"], "tenant_id")
            workspace_id = _require_identifier(requester["workspace_id"], "workspace_id")
        except ValueError as exc:
            raise IdentityBoundaryError(IdentityBoundaryReason.REQUESTER_IDENTITY_INVALID) from exc

        if tenant_id != self.tenant_id:
            raise IdentityBoundaryError(IdentityBoundaryReason.TENANT_MISMATCH)
        if workspace_id != self.workspace_id:
            raise IdentityBoundaryError(IdentityBoundaryReason.WORKSPACE_MISMATCH)
        if client_id not in self.requester_client_ids:
            raise IdentityBoundaryError(IdentityBoundaryReason.CLIENT_NOT_TRUSTED)

    def _validate_verified(
        self,
        principal: VerifiedPrincipal,
        *,
        now: datetime,
        trusted_clients: frozenset[str],
    ) -> None:
        current_time = _aware_utc(now, "now")
        if principal.expires_at <= current_time:
            raise IdentityBoundaryError(IdentityBoundaryReason.CREDENTIAL_EXPIRED)
        if principal.tenant_id != self.tenant_id:
            raise IdentityBoundaryError(IdentityBoundaryReason.TENANT_MISMATCH)
        if principal.workspace_id != self.workspace_id:
            raise IdentityBoundaryError(IdentityBoundaryReason.WORKSPACE_MISMATCH)
        if principal.client_id not in trusted_clients:
            raise IdentityBoundaryError(IdentityBoundaryReason.CLIENT_NOT_TRUSTED)
        if principal.audience not in self.trusted_audiences:
            raise IdentityBoundaryError(IdentityBoundaryReason.AUDIENCE_NOT_TRUSTED)
