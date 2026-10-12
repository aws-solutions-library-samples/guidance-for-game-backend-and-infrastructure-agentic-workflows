"""Unit tests for centralized, fail-closed operations settings (issue #413)."""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.settings import OperationsSettings, resolve_operations_settings


def test_default_is_disabled_and_read_only() -> None:
    settings = resolve_operations_settings(env={})
    assert settings.mode == "disabled"
    assert settings.operations_enabled is False
    assert settings.observe_enabled is False


def test_observe_mode_enables_observation() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    assert settings.observe_enabled is True
    assert settings.operations_enabled is True


@pytest.mark.parametrize("mode", ["advise", "remediate", "operate"])
def test_higher_modes_also_admit_observe(mode: str) -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": mode})
    assert settings.observe_enabled is True


def test_invalid_mode_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "root"})


def test_authority_ceiling_returns_the_lower_value() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    assert settings.authority_ceiling(requested="operate") == "observe"
    assert settings.authority_ceiling(requested="disabled") == "disabled"


def test_budgets_default_and_sum_below_gateway_ceiling() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    total = settings.per_read_budget_s * 3 + settings.persistence_budget_s + settings.cancellation_margin_s
    assert total < 30.0
    assert settings.observation_ttl_s == 1800


def test_invalid_budget_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_operations_settings(env={"GBAW_OPERATIONS_PER_READ_BUDGET_S": "-1"})


def test_invalid_ttl_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_operations_settings(env={"GBAW_OPERATIONS_OBSERVATION_TTL_S": "0"})


def test_settings_are_frozen() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    with pytest.raises(Exception):
        settings.mode = "operate"  # type: ignore[misc]


def test_construct_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        OperationsSettings(
            mode="nope",
            per_read_budget_s=3.0,
            persistence_budget_s=3.0,
            cancellation_margin_s=3.0,
            observation_ttl_s=1800,
            observer_groups=frozenset({"admin"}),
        )


def test_observer_groups_default_to_admin_and_users() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    assert settings.observer_groups == frozenset({"admin", "users"})


def test_observer_groups_parse_comma_separated_value() -> None:
    settings = resolve_operations_settings(
        env={"GBAW_OPERATIONS_MODE": "observe", "GBAW_OPERATIONS_OBSERVER_GROUPS": "ops, sre"}
    )
    assert settings.observer_groups == frozenset({"ops", "sre"})


def test_observer_groups_blank_value_uses_default() -> None:
    settings = resolve_operations_settings(
        env={"GBAW_OPERATIONS_MODE": "observe", "GBAW_OPERATIONS_OBSERVER_GROUPS": "   "}
    )
    assert settings.observer_groups == frozenset({"admin", "users"})


def test_observer_groups_invalid_token_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_operations_settings(
            env={"GBAW_OPERATIONS_MODE": "observe", "GBAW_OPERATIONS_OBSERVER_GROUPS": "ops,[bad]"}
        )


def test_construct_rejects_empty_observer_groups() -> None:
    with pytest.raises(ValueError):
        OperationsSettings(
            mode="observe",
            per_read_budget_s=3.0,
            persistence_budget_s=3.0,
            cancellation_margin_s=3.0,
            observation_ttl_s=1800,
            observer_groups=frozenset(),
        )


def test_total_deadline_matches_budget_components() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    expected = settings.per_read_budget_s * 3 + settings.persistence_budget_s + settings.cancellation_margin_s
    assert settings.total_deadline_s == expected
    assert settings.total_deadline_s == 15.0


def test_e2_disabled_and_observe_modes_deny_e2() -> None:
    for mode in ("disabled", "observe"):
        settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": mode})
        assert settings.e2_enabled is False


@pytest.mark.parametrize("mode", ["advise", "remediate", "operate"])
def test_advise_and_higher_modes_enable_e2(mode: str) -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": mode})
    # The advise ceiling enables E1 observe AND the E2 prepare/approval surface.
    assert settings.e2_enabled is True
    assert settings.observe_enabled is True
    assert settings.advise_enabled is True


# -- E2 approval settings (issue #414) --------------------------------------


def test_e2_settings_defaults_are_fail_closed() -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "advise"})
    # Self-approval is denied by default: an owner must opt in explicitly.
    assert settings.low_risk_self_approval_enabled is False
    # The two E2 lifecycle expiries default to sensible, positive windows.
    assert settings.preparation_expiry_s == 900
    assert settings.approval_expiry_s == 1800


def test_e2_low_risk_self_approval_opt_in_is_parsed() -> None:
    for raw, expected in (("true", True), ("false", False), ("1", True), ("0", False)):
        settings = resolve_operations_settings(
            env={"GBAW_OPERATIONS_MODE": "advise", "GBAW_OPERATIONS_LOW_RISK_SELF_APPROVAL": raw}
        )
        assert settings.low_risk_self_approval_enabled is expected


def test_e2_low_risk_self_approval_invalid_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_operations_settings(
            env={"GBAW_OPERATIONS_MODE": "advise", "GBAW_OPERATIONS_LOW_RISK_SELF_APPROVAL": "maybe"}
        )


def test_e2_expiries_are_overridable_and_positive() -> None:
    settings = resolve_operations_settings(
        env={
            "GBAW_OPERATIONS_MODE": "advise",
            "GBAW_OPERATIONS_PREPARATION_EXPIRY_S": "600",
            "GBAW_OPERATIONS_APPROVAL_EXPIRY_S": "1200",
        }
    )
    assert settings.preparation_expiry_s == 600
    assert settings.approval_expiry_s == 1200


@pytest.mark.parametrize("key", ["GBAW_OPERATIONS_PREPARATION_EXPIRY_S", "GBAW_OPERATIONS_APPROVAL_EXPIRY_S"])
def test_e2_expiry_invalid_fails_closed(key: str) -> None:
    with pytest.raises(ValueError):
        resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "advise", key: "0"})


@pytest.mark.parametrize("key", ["GBAW_OPERATIONS_PREPARATION_EXPIRY_S", "GBAW_OPERATIONS_APPROVAL_EXPIRY_S"])
def test_e2_expiry_below_the_floor_fails_closed_in_advise(key: str) -> None:
    # A window shorter than one store call limit plus a margin would fail every
    # prepare or approve on a healthy store, so an advise-mode deployment rejects
    # it rather than configuring an unusable window.
    with pytest.raises(ValueError):
        resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "advise", key: "4"})


@pytest.mark.parametrize(
    "key,attr",
    [
        ("GBAW_OPERATIONS_PREPARATION_EXPIRY_S", "preparation_expiry_s"),
        ("GBAW_OPERATIONS_APPROVAL_EXPIRY_S", "approval_expiry_s"),
    ],
)
def test_e2_expiry_at_the_floor_is_accepted_in_advise(key: str, attr: str) -> None:
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "advise", key: "60"})
    assert getattr(settings, attr) == 60


@pytest.mark.parametrize("key", ["GBAW_OPERATIONS_PREPARATION_EXPIRY_S", "GBAW_OPERATIONS_APPROVAL_EXPIRY_S"])
def test_e2_expiry_floor_is_inert_in_observe(key: str) -> None:
    # Below the advise ceiling the E2 windows are inert, so observe mode keeps
    # accepting a short value rather than enforcing the floor.
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe", key: "4"})
    assert settings.e2_enabled is False


def test_approval_expiry_setting_feeds_the_approval_policy_ttl() -> None:
    # The configured approval expiry window is consumed as the approval policy's
    # granted-approval validity, rather than being parsed and ignored.
    # Standard library
    from datetime import timedelta

    # Local modules
    from operations.approval import ApprovalPolicy

    settings = resolve_operations_settings(
        env={"GBAW_OPERATIONS_MODE": "advise", "GBAW_OPERATIONS_APPROVAL_EXPIRY_S": "600"}
    )
    assert settings.approval_expiry_s == 600

    policy = ApprovalPolicy(
        policy_id="policy.gamelift.capacity",
        policy_version="1",
        approver_scopes=frozenset({"operations-api"}),
        approval_ttl=timedelta(seconds=settings.approval_expiry_s),
    )
    assert policy.approval_ttl == timedelta(seconds=600)


def test_approver_group_is_validated_against_the_group_token_alphabet() -> None:
    # A malformed approver group (space, control, or injection characters) fails
    # closed, the same way an observer group would.
    with pytest.raises(ValueError):
        resolve_operations_settings(
            env={"GBAW_OPERATIONS_MODE": "advise", "GBAW_OPERATIONS_APPROVER_GROUP": "bad group!"}
        )


def test_valid_approver_group_is_accepted() -> None:
    settings = resolve_operations_settings(
        env={"GBAW_OPERATIONS_MODE": "advise", "GBAW_OPERATIONS_APPROVER_GROUP": "capacity.approvers"}
    )
    assert settings.approver_group == "capacity.approvers"


# --------------------------------------------------------------------------- #
# E2 fields are parsed and validated ONLY when the mode admits the E2 surface,
# so a malformed E2 value can never raise at load time and break observe mode
# (issue #414).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "env_name",
    [
        "GBAW_OPERATIONS_PREPARATION_EXPIRY_S",
        "GBAW_OPERATIONS_APPROVAL_EXPIRY_S",
        "GBAW_OPERATIONS_CAPACITY_FLOOR",
        "GBAW_OPERATIONS_CAPACITY_CEILING",
        "GBAW_OPERATIONS_CAPACITY_MAX_STEP",
    ],
)
def test_observe_mode_ignores_a_malformed_e2_value(env_name: str) -> None:
    # In observe mode the E2 environment is not parsed, so a malformed E2 value
    # (a fraction, a non-integer) is ignored and the safe default is held — it
    # must NOT raise and take observe mode down.
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe", env_name: "900.5"})
    assert settings.observe_enabled is True
    assert settings.e2_enabled is False
    # The E2 fields hold their safe defaults.
    assert settings.preparation_expiry_s == 900
    assert settings.approval_expiry_s == 1800
    assert (settings.capacity_floor, settings.capacity_ceiling, settings.capacity_max_step) == (0, 1, 1)


def test_advise_mode_still_validates_a_malformed_e2_value() -> None:
    # In advise mode the E2 fields ARE parsed and validated, so a malformed value
    # fails closed at load time.
    with pytest.raises(ValueError):
        resolve_operations_settings(
            env={"GBAW_OPERATIONS_MODE": "advise", "GBAW_OPERATIONS_PREPARATION_EXPIRY_S": "900.5"}
        )


def test_advise_mode_reads_e2_overrides() -> None:
    settings = resolve_operations_settings(
        env={
            "GBAW_OPERATIONS_MODE": "advise",
            "GBAW_OPERATIONS_PREPARATION_EXPIRY_S": "600",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "8",
            "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "2",
        }
    )
    assert settings.e2_enabled is True
    assert settings.preparation_expiry_s == 600
    assert settings.capacity_ceiling == 8
    assert settings.capacity_max_step == 2
