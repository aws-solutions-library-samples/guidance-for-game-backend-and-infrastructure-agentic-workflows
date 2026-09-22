"""Strict, shared parser for API Gateway JWT authorizer claim values (#414).

Why this module exists
----------------------
The E1 (observation) and E2 (approval) Lambda handlers trust identity **only**
from the API Gateway HTTP API (payload format 2.0) JWT authorizer context at
``requestContext.authorizer.jwt.claims``. API Gateway itself decodes the Cognito
access token, verifies its signature and standard claims, and then forwards the
claims to the Lambda integration. Crucially, that authorizer context is a flat
``map[string, string]`` — every claim value is delivered to the integration as a
**string**, even claims that were JSON arrays in the original token.

For Amazon Cognito the ``cognito:groups`` claim is a JSON *array* of group names
in the token (see the Cognito Developer Guide, "Adding groups to a user pool" —
https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-user-groups.html).
When the HTTP API JWT authorizer flattens that array into the string map it
forwards to the integration, a multi-value array is rendered in a *bracketed*
form: a single group ``admin`` arrives as the literal string ``"[admin]"`` and
two groups arrive as ``"[admin users]"`` (bracket-wrapped, space-separated). The
brackets are part of the delivered string, not JSON that the integration
re-parses.

The previous per-handler ``_string_set`` helper simply called ``str.split()`` on
that value, so ``"[admin]"`` became the single token ``"[admin]"`` — which never
matches the configured approver group ``"admin"``. A user whose decoded token
genuinely carried ``groups=["admin"]`` was therefore denied with 403 at the live
distinct-approver boundary even though the same claims authorized locally (where
the identity is a real Python list off a JWKS-decoded token). This module is the
single, shared, strict parser that both handlers use so the two representations —
a genuine list/tuple and the authorizer's bracketed string — reduce to the same
group set, while any malformed, nested, object, or oversized input fails closed.

Representations accepted for ``cognito:groups`` (see ``parse_group_claim``):

* a direct ``list``/``tuple``/``frozenset`` of group-name strings (the shape
  seen when the claims come from a genuine JSON decode, e.g. the AgentCore
  runtime path), and
* the authorizer's flattened *bracketed* string form: ``"[admin]"``,
  ``"[admin users]"``, and — defensively — a comma-delimited variant inside the
  brackets (``"[admin, users]"``). A bare (bracketless) comma- or
  space-delimited string is also accepted so a future authorizer/CloudFormation
  mapping-template form is tolerated, but nested brackets, JSON objects, and
  oversized payloads are rejected rather than best-effort parsed.

``scope`` is a distinct OAuth 2.0 claim that is *always* a single
space-delimited string (RFC 6749 §3.3). It is parsed by ``parse_scope_claim``
and is never bracket-interpreted, so a group token can never be mistaken for a
scope or vice versa.

Every helper is total and side-effect free: it either returns a bounded
``frozenset[str]`` of validated tokens or raises :class:`ClaimParseError`. It
never returns a partially-parsed set for malformed input.
"""

from __future__ import annotations

# Standard library
from collections.abc import Sequence

# A single group/scope token is bounded to keep the parser total and cheap and to
# match the per-token cap already enforced downstream by the identity value
# objects. Cognito group names are far shorter than this in practice.
_MAX_TOKEN_LENGTH = 256

# The whole raw claim string is bounded before any parsing so a hostile or
# corrupt authorizer value cannot force unbounded work. A realistic
# ``cognito:groups`` string holds a handful of short names; 4 KiB is generous.
_MAX_RAW_LENGTH = 4096

# A verified principal never legitimately carries an unbounded number of groups
# or scopes. Bounding the token count fails closed on a pathological value.
_MAX_TOKENS = 64


class ClaimParseError(ValueError):
    """A claim value could not be parsed into a bounded, valid token set.

    Raised for malformed, nested, object-typed, or oversized input. Handlers map
    it to their typed ``IDENTITY_CONTEXT_INVALID`` boundary error so a corrupt or
    hostile authorizer value fails closed instead of silently authorizing.
    """


def _validate_token(token: str) -> str:
    """Return ``token`` if it is a valid single group/scope token, else raise.

    A token must be a nonempty string with no surrounding whitespace, no residual
    bracket characters (which would indicate a nested/malformed structure that
    escaped splitting), and within the per-token length bound.
    """
    if not isinstance(token, str):
        raise ClaimParseError("claim token must be a string")
    if not token or token != token.strip():
        raise ClaimParseError("claim token must be a nonempty trimmed string")
    if len(token) > _MAX_TOKEN_LENGTH:
        raise ClaimParseError("claim token exceeds the maximum length")
    if "[" in token or "]" in token:
        raise ClaimParseError("claim token contains a residual bracket")
    return token


def _freeze(tokens: Sequence[str]) -> frozenset[str]:
    """Validate and freeze an already-split sequence of tokens, failing closed."""
    if len(tokens) > _MAX_TOKENS:
        raise ClaimParseError("claim carries too many tokens")
    return frozenset(_validate_token(token) for token in tokens)


def _split_string_claim(raw: str) -> list[str]:
    """Split one raw authorizer claim *string* into candidate tokens.

    Handles the API Gateway HTTP API flattened bracketed array form
    (``"[admin]"`` / ``"[admin users]"``) as well as a bare space- or
    comma-delimited string. Exactly one pair of *outer* brackets is stripped;
    any bracket character surviving inside a token is caught by
    :func:`_validate_token` so a nested form (``"[[admin]]"``,
    ``"[a],[b]"``) fails closed.
    """
    if len(raw) > _MAX_RAW_LENGTH:
        raise ClaimParseError("claim string exceeds the maximum length")
    trimmed = raw.strip()
    if not trimmed:
        return []

    # Strip at most one balanced outer bracket pair — the authorizer's flattened
    # array wrapper. A leading-only or trailing-only bracket is malformed.
    has_open = trimmed.startswith("[")
    has_close = trimmed.endswith("]")
    if has_open != has_close:
        raise ClaimParseError("claim string has an unbalanced bracket")
    if has_open and has_close:
        trimmed = trimmed[1:-1].strip()
        if not trimmed:
            # ``"[]"`` — an explicitly empty array of groups.
            return []

    # Inside the (optional) brackets both AWS-observed and defensive delimiters
    # are accepted: commas first (so ``"[admin, users]"`` works), then any
    # remaining whitespace. Empty fragments from doubled delimiters are dropped
    # here; genuine malformed tokens are still rejected by ``_validate_token``.
    fragments: list[str] = []
    for comma_part in trimmed.split(","):
        fragments.extend(comma_part.split())
    return [fragment for fragment in fragments if fragment]


def parse_group_claim(value: object) -> frozenset[str]:
    """Parse a ``cognito:groups`` claim value into a bounded set of group names.

    Accepts a direct ``list``/``tuple``/``frozenset`` of strings (genuine JSON
    decode) or the API Gateway JWT authorizer's flattened bracketed string form.
    A missing claim (``None``) is an empty group set. Any other shape — a dict,
    a non-string element, a nested/unbalanced bracket structure, or an oversized
    value — raises :class:`ClaimParseError` so the caller fails closed.
    """
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, frozenset, set)):
        return _freeze([_require_str_element(element) for element in value])
    if isinstance(value, str):
        return _freeze(_split_string_claim(value))
    raise ClaimParseError("cognito:groups claim has an unsupported type")


def parse_scope_claim(value: object) -> frozenset[str]:
    """Parse an OAuth 2.0 ``scope`` claim into a bounded set of scope tokens.

    ``scope`` is always a single space-delimited string (RFC 6749 §3.3); a
    ``list``/``tuple``/``frozenset`` of scope strings is also accepted for the
    genuine-decode path. Unlike groups, a scope string is **never** bracket- or
    comma-interpreted, so a group token can never be confused for a scope. A
    missing claim is an empty scope set; any other shape fails closed.
    """
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, frozenset, set)):
        return _freeze([_require_str_element(element) for element in value])
    if isinstance(value, str):
        if len(value) > _MAX_RAW_LENGTH:
            raise ClaimParseError("scope claim exceeds the maximum length")
        return _freeze(value.split())
    raise ClaimParseError("scope claim has an unsupported type")


def _require_str_element(element: object) -> str:
    """Return ``element`` if it is a string, else fail closed.

    A non-string element in a list/tuple claim (e.g. a nested list or an object)
    is malformed rather than best-effort skipped, so the whole claim is rejected.
    """
    if not isinstance(element, str):
        raise ClaimParseError("claim collection contains a non-string element")
    return element
