"""Behavioral tests for the hosted Cognito JWT identity boundary."""

# Standard library
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

# Third-party packages
import jwt
import pytest
from bedrock_agentcore.runtime.context import RequestContext
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.unit

AUTHORIZATION_HEADER = "Authorization"
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example"
CLIENT_ID = "example-client"
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()


def _token(**overrides: object) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "sub": "subject-123",
        "iss": ISSUER,
        "client_id": CLIENT_ID,
        "token_use": "access",
        "exp": now + timedelta(minutes=5),
        "iat": now - timedelta(seconds=5),
        "scope": "openid profile",
        "cognito:groups": ["users", "source-readers"],
    }
    claims.update(overrides)
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


def _context(token: str | None, *, scheme: str = "Bearer") -> RequestContext:
    headers = {} if token is None else {AUTHORIZATION_HEADER: f"{scheme} {token}"}
    return RequestContext(session_id="session-1", request_headers=headers)


def _verify(context: RequestContext):
    # Local modules
    from runtime_identity import verify_cognito_runtime_identity

    with patch("runtime_identity._jwk_client") as jwk_client:
        jwk_client.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=_PUBLIC_KEY)
        return verify_cognito_runtime_identity(context, issuer=ISSUER, client_id=CLIENT_ID)


def test_valid_access_token_establishes_immutable_runtime_identity():
    identity = _verify(_context(_token()))

    assert identity.subject_id == "subject-123"
    assert identity.client_id == CLIENT_ID
    assert identity.groups == frozenset({"users", "source-readers"})
    assert identity.scopes == frozenset({"openid", "profile"})


@pytest.mark.parametrize(
    ("token", "scheme"),
    [
        (None, "Bearer"),
        (_token(), "Basic"),
        ("not-a-jwt", "Bearer"),
    ],
    ids=["missing", "wrong-scheme", "malformed"],
)
def test_missing_or_malformed_bearer_credential_fails_closed(token: str | None, scheme: str):
    # Local modules
    from runtime_identity import RuntimeIdentityError

    with pytest.raises(RuntimeIdentityError):
        _verify(_context(token, scheme=scheme))


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_id": "other-client"},
        {"token_use": "id"},
        {"iss": "https://issuer.invalid"},
        {"sub": ""},
        {"exp": datetime.now(timezone.utc) - timedelta(seconds=60)},
    ],
)
def test_untrusted_claims_fail_closed(overrides: dict[str, object]):
    # Local modules
    from runtime_identity import RuntimeIdentityError

    with pytest.raises(RuntimeIdentityError):
        _verify(_context(_token(**overrides)))


def test_body_identity_is_not_an_input_to_jwt_verification():
    """The verifier accepts only the platform-forwarded Authorization header."""
    identity = _verify(_context(_token(sub="trusted-subject")))

    assert identity.subject_id == "trusted-subject"
    assert not hasattr(identity, "body_user_id")
