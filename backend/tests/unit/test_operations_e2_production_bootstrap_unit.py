"""Production-bootstrap safety tests for the E2 control plane (issue #414).

These tests pin the three security-critical bootstrap decisions the deployable
E2 handler must make *before* it is enabled against real infrastructure:

1. **Approver authority is the server-owned Cognito ``admin`` group, not the
   trusted app client id.** A real access token's ``scope`` claim is not the
   Cognito client id, so binding approver authority to the client id (via
   ``approver_scopes``) would let any in-audience caller approve. The bootstrap
   policy must require the ``admin`` group. Default ``users`` requesters cannot
   approve; a distinct ``admin`` can. Low-risk self-approval, when enabled,
   still requires approver-group authority.
2. **The code-owned capacity playbook hash is a real RFC 8785 / SHA-256 digest
   of the complete immutable playbook definition** — never an all-zero
   placeholder — and any drift in the definition changes the hash.
3. **Capacity bounds are server-owned fail-closed settings** (safe default
   0/1/1) injected into the resolver, never hardcoded million-instance limits.

The tests use the real Cognito group/scope shapes the on-host adapter produces
(``cognito:groups`` -> ``groups``, ``scope`` -> ``scopes``).
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.approval import ApprovalPolicy, ApprovalPolicyError, ApprovalPolicyReason
from operations.contracts.canonical import canonical_sha256
from operations.identity import VerifiedPrincipal
from operations.playbook_definition import CAPACITY_PLAYBOOK_DEFINITION, capacity_playbook_hash
from operations.settings import resolve_operations_settings

NOW = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
CLIENT_ID = "client.operations-web"
AUDIENCE = "client.operations-web"

# The exact expected digest of the complete immutable playbook definition. Any
# definition or serialization drift changes this and fails the vector test.
_EXPECTED_PLAYBOOK_HASH = "sha256:553649474fd2c1d0340bca1d0901c389b65b45baefc3c87846e6b83ed4e2e2a3"


# --------------------------------------------------------------------------- #
# 1. Approver authority is the server-owned admin group, not the client id.
# --------------------------------------------------------------------------- #


def _prepared_operation(*, requester_subject: str, action: str, risk_level: str = "high") -> dict:
    return {
        "action": action,
        "policy": {"policy_id": "policy.gamelift.capacity", "policy_version": "1"},
        "requester": {"subject_id": requester_subject},
        "calculated_risk": {"level": risk_level},
    }


def _principal(*, subject: str, groups: frozenset[str], scopes: frozenset[str]) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject_id=subject,
        client_id=CLIENT_ID,
        audience=AUDIENCE,
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=NOW + timedelta(minutes=30),
        groups=groups,
        scopes=scopes,
    )


def _admin_group_policy(*, low_risk_actions: frozenset[str] = frozenset()) -> ApprovalPolicy:
    return ApprovalPolicy(
        policy_id="policy.gamelift.capacity",
        policy_version="1",
        approver_groups=frozenset({"admin"}),
        approver_scopes=frozenset(),
        low_risk_self_approval_actions=low_risk_actions,
    )


def test_default_users_requester_cannot_approve_even_in_trusted_audience() -> None:
    """A ``users`` caller whose scope equals the client id must not approve.

    This is the exact failure mode of binding approver authority to the client
    id: the caller is a member of ``users`` (not ``admin``) and carries a scope
    claim that equals the trusted app client id, yet must be denied.
    """
    policy = _admin_group_policy()
    approver = _principal(
        subject="subject.regular-user",
        groups=frozenset({"users"}),
        scopes=frozenset({CLIENT_ID}),  # scope == client id must NOT authorize
    )
    prepared = _prepared_operation(requester_subject="subject.someone-else", action="gamelift.adjust-fleet-capacity")

    with pytest.raises(ApprovalPolicyError) as exc:
        policy.authorize(prepared, approver)
    assert exc.value.reason is ApprovalPolicyReason.APPROVER_NOT_AUTHORIZED


def test_distinct_admin_can_approve_another_users_operation() -> None:
    policy = _admin_group_policy()
    admin = _principal(subject="subject.admin", groups=frozenset({"admin"}), scopes=frozenset())
    prepared = _prepared_operation(requester_subject="subject.regular-user", action="gamelift.adjust-fleet-capacity")

    # Distinct admin approving another requester's operation is allowed.
    policy.authorize(prepared, admin)


def test_low_risk_self_approval_still_requires_admin_group() -> None:
    """An explicit low-risk self-approval opt-in still needs approver authority.

    A requester in ``users`` approving their own low-risk operation is denied
    for lack of the ``admin`` group before the self-approval branch is reached.
    """
    action = "gamelift.adjust-fleet-capacity"
    policy = _admin_group_policy(low_risk_actions=frozenset({action}))
    requester_in_users = _principal(subject="subject.self", groups=frozenset({"users"}), scopes=frozenset({CLIENT_ID}))
    prepared = _prepared_operation(requester_subject="subject.self", risk_level="low", action=action)

    with pytest.raises(ApprovalPolicyError) as exc:
        policy.authorize(prepared, requester_in_users)
    assert exc.value.reason is ApprovalPolicyReason.APPROVER_NOT_AUTHORIZED


def test_low_risk_self_approval_allowed_for_admin_requester_when_opted_in() -> None:
    action = "gamelift.adjust-fleet-capacity"
    policy = _admin_group_policy(low_risk_actions=frozenset({action}))
    admin_self = _principal(subject="subject.admin", groups=frozenset({"admin"}), scopes=frozenset())
    prepared = _prepared_operation(requester_subject="subject.admin", risk_level="low", action=action)

    # Explicit low-risk self-approval by an admin is permitted.
    policy.authorize(prepared, admin_self)


def test_admin_self_approval_denied_when_not_opted_in() -> None:
    action = "gamelift.adjust-fleet-capacity"
    policy = _admin_group_policy(low_risk_actions=frozenset())
    admin_self = _principal(subject="subject.admin", groups=frozenset({"admin"}), scopes=frozenset())
    prepared = _prepared_operation(requester_subject="subject.admin", risk_level="low", action=action)

    with pytest.raises(ApprovalPolicyError) as exc:
        policy.authorize(prepared, admin_self)
    assert exc.value.reason is ApprovalPolicyReason.SELF_APPROVAL_NOT_ALLOWED


def test_admin_self_approval_denied_when_not_low_risk() -> None:
    action = "gamelift.adjust-fleet-capacity"
    policy = _admin_group_policy(low_risk_actions=frozenset({action}))
    admin_self = _principal(subject="subject.admin", groups=frozenset({"admin"}), scopes=frozenset())
    prepared = _prepared_operation(requester_subject="subject.admin", risk_level="high", action=action)

    with pytest.raises(ApprovalPolicyError) as exc:
        policy.authorize(prepared, admin_self)
    assert exc.value.reason is ApprovalPolicyReason.SELF_APPROVAL_RISK_NOT_ELIGIBLE


# --------------------------------------------------------------------------- #
# 2. Real playbook hash over the complete immutable definition.
# --------------------------------------------------------------------------- #


def test_playbook_hash_is_not_the_all_zero_placeholder() -> None:
    digest = capacity_playbook_hash()
    assert digest != "sha256:" + "0" * 64
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_playbook_hash_is_the_canonical_sha256_of_the_definition() -> None:
    assert capacity_playbook_hash() == canonical_sha256(CAPACITY_PLAYBOOK_DEFINITION)


def test_playbook_hash_is_a_known_fixed_vector() -> None:
    # Pins the exact digest so any definition or serialization drift fails here.
    assert capacity_playbook_hash() == _EXPECTED_PLAYBOOK_HASH


def test_playbook_definition_carries_the_complete_immutable_binding() -> None:
    definition = CAPACITY_PLAYBOOK_DEFINITION
    assert definition["playbook_id"] == "playbook.gamelift-capacity"
    assert definition["playbook_version"] == "1.0.0"
    assert definition["profile"] == "gamelift.capacity-adjustment/1.0"
    assert definition["capability"] == {
        "capability_id": "gamelift.capacity-adjustment",
        "capability_version": "1.0",
    }
    assert definition["retry_policy"] == {
        "max_attempts": 3,
        "base_delay_seconds": 2,
        "max_delay_seconds": 60,
        "reconcile_before_retry": True,
    }
    assert definition["future_executor_binding"] == {
        "executor_id": "executor.gamelift-capacity",
        "executor_binding_version": "1.0",
    }
    assert set(definition["parameter_bounds"]) == {"desired", "minimum", "maximum"}
    assert "preconditions" in definition
    # The immutable execution authority a future E3 executor re-verifies before
    # any write is frozen into the playbook and therefore into its hash.
    assert definition["required_execution_authority"] == "remediate"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("playbook_version", "1.0.1"),
        lambda d: d.__setitem__("profile", "other/1.0"),
        lambda d: d["retry_policy"].__setitem__("max_attempts", 4),
        lambda d: d["future_executor_binding"].__setitem__("executor_binding_version", "2.0"),
        lambda d: d["parameter_bounds"].__setitem__("desired", {"minimum": -1, "maximum": 999}),
        lambda d: d["preconditions"].append("EXTRA_PRECONDITION"),
        lambda d: d["capability"].__setitem__("capability_version", "1.1"),
        lambda d: d.__setitem__("required_execution_authority", "operate"),
    ],
)
def test_any_definition_drift_changes_the_hash(mutate) -> None:
    drifted = deepcopy(CAPACITY_PLAYBOOK_DEFINITION)
    mutate(drifted)
    assert canonical_sha256(drifted) != capacity_playbook_hash()


# --------------------------------------------------------------------------- #
# 3. Server-owned fail-closed capacity bounds settings.
# --------------------------------------------------------------------------- #

_BASE_ENV = {"GBAW_OPERATIONS_MODE": "advise"}


def test_capacity_bounds_default_to_safe_fail_closed_values() -> None:
    settings = resolve_operations_settings(dict(_BASE_ENV))
    assert settings.capacity_floor == 0
    assert settings.capacity_ceiling == 1
    assert settings.capacity_max_step == 1


def test_capacity_bounds_are_read_from_the_exact_env_names() -> None:
    env = dict(_BASE_ENV)
    env.update(
        {
            "GBAW_OPERATIONS_CAPACITY_FLOOR": "2",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "10",
            "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "3",
        }
    )
    settings = resolve_operations_settings(env)
    assert (settings.capacity_floor, settings.capacity_ceiling, settings.capacity_max_step) == (2, 10, 3)


@pytest.mark.parametrize(
    "overrides",
    [
        {"GBAW_OPERATIONS_CAPACITY_FLOOR": "-1"},
        {"GBAW_OPERATIONS_CAPACITY_MAX_STEP": "0"},
        {"GBAW_OPERATIONS_CAPACITY_MAX_STEP": "-2"},
        {"GBAW_OPERATIONS_CAPACITY_FLOOR": "5", "GBAW_OPERATIONS_CAPACITY_CEILING": "4"},
        {"GBAW_OPERATIONS_CAPACITY_FLOOR": "1.5"},
        {"GBAW_OPERATIONS_CAPACITY_CEILING": "abc"},
        {
            "GBAW_OPERATIONS_CAPACITY_FLOOR": "0",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "2",
            "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "3",
        },
    ],
)
def test_invalid_capacity_bounds_fail_closed(overrides) -> None:
    env = dict(_BASE_ENV)
    env.update(overrides)
    with pytest.raises(ValueError):
        resolve_operations_settings(env)


def test_zero_ceiling_with_zero_floor_is_coherent() -> None:
    env = dict(_BASE_ENV)
    env.update({"GBAW_OPERATIONS_CAPACITY_FLOOR": "0", "GBAW_OPERATIONS_CAPACITY_CEILING": "0"})
    settings = resolve_operations_settings(env)
    # Fully clamped: floor==ceiling==0, max_step default 1 <= span+1 is fine.
    assert (settings.capacity_floor, settings.capacity_ceiling) == (0, 0)


# --------------------------------------------------------------------------- #
# 4. Approver group is a server-owned setting (default admin).
# --------------------------------------------------------------------------- #


def test_approver_group_defaults_to_admin() -> None:
    settings = resolve_operations_settings(dict(_BASE_ENV))
    assert settings.approver_group == "admin"


def test_approver_group_is_overridable_via_env() -> None:
    env = dict(_BASE_ENV)
    env["GBAW_OPERATIONS_APPROVER_GROUP"] = "operations-approvers"
    settings = resolve_operations_settings(env)
    assert settings.approver_group == "operations-approvers"


def test_blank_approver_group_falls_back_to_admin_default() -> None:
    env = dict(_BASE_ENV)
    env["GBAW_OPERATIONS_APPROVER_GROUP"] = "   "
    settings = resolve_operations_settings(env)
    assert settings.approver_group == "admin"
