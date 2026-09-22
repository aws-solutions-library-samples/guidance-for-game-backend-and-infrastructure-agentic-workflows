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
        )


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
