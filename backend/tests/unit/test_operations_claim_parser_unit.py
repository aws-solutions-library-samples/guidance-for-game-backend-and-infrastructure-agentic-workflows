"""Unit tests for the shared strict claim parser (issue #414).

These tests pin the parser against the concrete representations an API Gateway
HTTP API (payload format 2.0) JWT authorizer delivers to a Lambda integration in
``requestContext.authorizer.jwt.claims``. The authorizer forwards claims as a
flat string map, so a Cognito ``cognito:groups`` JSON *array* arrives flattened
into a *bracketed* string: one group is ``"[admin]"`` and several are
``"[admin users]"`` (bracket-wrapped, space-separated). See the Cognito
Developer Guide, "Adding groups to a user pool"
(https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-user-groups.html)
for the array nature of the claim, and the API Gateway JWT authorizer guide
(https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html)
for how the verified claims are passed to the integration.

The core regression: the old ``str.split()`` helper turned ``"[admin]"`` into the
single token ``"[admin]"``, which never matched the configured approver group
``"admin"`` — the live 403. ``parse_group_claim`` must strip the wrapper and
yield ``{"admin"}``, while failing closed on malformed/nested/object/oversized
input. ``parse_scope_claim`` must remain space-delimited only and must never
bracket-interpret a value, so a group form is never mistaken for a scope.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.claims import (
    ClaimParseError,
    parse_group_claim,
    parse_scope_claim,
)

pytestmark = [pytest.mark.unit]


class TestParseGroupClaimAuthorizerStringForm:
    """The bracketed string form API Gateway actually delivers."""

    def test_single_group_bracketed_string_strips_to_bare_name(self) -> None:
        # The regression case: decoded token had groups=["admin"], the
        # authorizer flattened it to the literal string "[admin]".
        assert parse_group_claim("[admin]") == frozenset({"admin"})

    def test_multiple_groups_bracketed_space_separated(self) -> None:
        assert parse_group_claim("[admin users]") == frozenset({"admin", "users"})

    def test_multiple_groups_bracketed_comma_separated(self) -> None:
        # Defensive: a comma-delimited variant inside the brackets is tolerated.
        assert parse_group_claim("[admin, users]") == frozenset({"admin", "users"})

    def test_empty_bracket_array_is_no_groups(self) -> None:
        assert parse_group_claim("[]") == frozenset()

    def test_users_only_does_not_contain_admin(self) -> None:
        parsed = parse_group_claim("[users]")
        assert parsed == frozenset({"users"})
        assert "admin" not in parsed

    def test_bare_space_delimited_string_without_brackets(self) -> None:
        # A future/bare authorizer mapping form is tolerated.
        assert parse_group_claim("admin users") == frozenset({"admin", "users"})


class TestParseGroupClaimDirectCollection:
    """The genuine JSON-decode path (a real Python list/tuple)."""

    def test_direct_list(self) -> None:
        assert parse_group_claim(["admin", "users"]) == frozenset({"admin", "users"})

    def test_direct_tuple(self) -> None:
        assert parse_group_claim(("admin",)) == frozenset({"admin"})

    def test_direct_frozenset(self) -> None:
        assert parse_group_claim(frozenset({"admin"})) == frozenset({"admin"})

    def test_missing_claim_is_empty(self) -> None:
        assert parse_group_claim(None) == frozenset()


class TestParseGroupClaimFailsClosed:
    """Malformed, nested, object, or oversized input must raise, not best-effort."""

    def test_unbalanced_open_bracket(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim("[admin")

    def test_unbalanced_close_bracket(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim("admin]")

    def test_nested_brackets_fail_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim("[[admin]]")

    def test_comma_separated_bracket_tokens_fail_closed(self) -> None:
        # "[a],[b]" would leave residual brackets in tokens -> rejected.
        with pytest.raises(ClaimParseError):
            parse_group_claim("[a],[b]")

    def test_object_claim_fails_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim({"group": "admin"})

    def test_non_string_element_in_list_fails_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim(["admin", 7])

    def test_nested_list_element_fails_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim([["admin"]])

    def test_oversized_string_fails_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim("[" + ("a" * 5000) + "]")

    def test_too_many_tokens_fail_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim("[" + " ".join(f"g{i}" for i in range(65)) + "]")

    def test_oversized_single_token_fails_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_group_claim("[" + ("a" * 257) + "]")


class TestParseScopeClaim:
    """Scopes are space-delimited only and never bracket-interpreted."""

    def test_space_delimited_scopes(self) -> None:
        assert parse_scope_claim("operations-api read") == frozenset({"operations-api", "read"})

    def test_single_scope(self) -> None:
        assert parse_scope_claim("operations-api") == frozenset({"operations-api"})

    def test_missing_scope_is_empty(self) -> None:
        assert parse_scope_claim(None) == frozenset()
        assert parse_scope_claim("") == frozenset()

    def test_scope_does_not_strip_brackets(self) -> None:
        # A scope is NEVER bracket-interpreted: a literal bracketed value stays a
        # residual-bracket token and fails closed, so a group-shaped value can
        # never be silently accepted as a scope.
        with pytest.raises(ClaimParseError):
            parse_scope_claim("[operations-api]")

    def test_group_form_is_not_confused_with_scope(self) -> None:
        # The same "[admin]" string parses as a group but must not yield a scope
        # set of {"admin"} — proving groups and scopes cannot be confused.
        assert parse_group_claim("[admin]") == frozenset({"admin"})
        with pytest.raises(ClaimParseError):
            parse_scope_claim("[admin]")

    def test_scope_object_fails_closed(self) -> None:
        with pytest.raises(ClaimParseError):
            parse_scope_claim({"scope": "read"})
