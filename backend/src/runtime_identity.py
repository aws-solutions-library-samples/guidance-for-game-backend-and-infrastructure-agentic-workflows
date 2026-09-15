"""Cryptographically verified inbound identity for hosted AgentCore requests.

Hosted production runtimes use AgentCore JWT bearer authorization. AgentCore
validates the Cognito token before forwarding it, and this module independently
verifies the same access token before any claim is admitted to application
state or authorization scope.
"""

from __future__ import annotations

# Standard library
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

# Third-party packages
import jwt
from jwt import PyJWKClient

_AUTHORIZATION_HEADER = "authorization"
_SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


class RuntimeIdentityError(ValueError):
    """The request did not carry a valid trusted runtime identity."""

    def __init__(self) -> None:
        super().__init__("runtime identity verification failed")


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Minimal identity admitted from a verified Cognito access token."""

    subject_id: str
    client_id: str
    groups: frozenset[str]
    scopes: frozenset[str]


def _lookup_header(headers: object, name: str) -> str | None:
    if not headers or not hasattr(headers, "items"):
        return None
    target = name.casefold()
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == target and isinstance(value, str):
            return value.strip() or None
    return None


def _authorization_header(context: object) -> str:
    if context is None:
        raise RuntimeIdentityError()

    value = _lookup_header(getattr(context, "request_headers", None), _AUTHORIZATION_HEADER)
    if not value:
        request = getattr(context, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            try:
                raw_value = headers.get(_AUTHORIZATION_HEADER)
                value = raw_value.strip() if isinstance(raw_value, str) else None
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                raise RuntimeIdentityError() from exc

    if not value:
        raise RuntimeIdentityError()
    return value


@lru_cache(maxsize=4)
def _jwk_client(issuer: str) -> PyJWKClient:
    return PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=300)


def _string_set(value: object) -> frozenset[str]:
    if isinstance(value, str):
        values = value.split()
    elif isinstance(value, list):
        values = value
    else:
        return frozenset()

    normalized = frozenset(item for item in values if isinstance(item, str) and item and len(item) <= 256)
    if len(normalized) != len(values):
        raise RuntimeIdentityError()
    return normalized


def verify_cognito_runtime_identity(
    context: object,
    *,
    issuer: str,
    client_id: str,
) -> RuntimeIdentity:
    """Verify and reduce the Cognito access token forwarded by AgentCore.

    The caller must provide deployment-owned issuer/client bindings. Body claims
    and AgentCore custom passthrough headers are intentionally not inputs.
    """
    normalized_issuer = issuer.strip().rstrip("/") if isinstance(issuer, str) else ""
    normalized_client_id = client_id.strip() if isinstance(client_id, str) else ""
    if not normalized_issuer.startswith("https://") or not normalized_client_id:
        raise RuntimeIdentityError()

    authorization = _authorization_header(context)
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token.strip():
        raise RuntimeIdentityError()
    token = token.strip()

    try:
        signing_key = _jwk_client(normalized_issuer).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=normalized_issuer,
            options={
                "verify_aud": False,
                "require": ["sub", "iss", "client_id", "token_use", "exp", "iat"],
            },
            leeway=30,
        )
    except Exception as exc:
        raise RuntimeIdentityError() from exc

    if not isinstance(claims, Mapping):
        raise RuntimeIdentityError()

    subject = claims.get("sub")
    token_client = claims.get("client_id")
    token_use = claims.get("token_use")
    if not isinstance(subject, str) or not _SUBJECT_PATTERN.fullmatch(subject):
        raise RuntimeIdentityError()
    if token_client != normalized_client_id or token_use != "access":
        raise RuntimeIdentityError()

    return RuntimeIdentity(
        subject_id=subject,
        client_id=normalized_client_id,
        groups=_string_set(claims.get("cognito:groups", [])),
        scopes=_string_set(claims.get("scope", "")),
    )
