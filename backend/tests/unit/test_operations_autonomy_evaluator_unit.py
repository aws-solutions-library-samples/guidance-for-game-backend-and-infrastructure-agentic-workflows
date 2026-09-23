"""Pure deterministic policy-evaluator tests for the E5 autonomy layer (#438).

``evaluate_autonomy_policy`` is a total function of trusted inputs only. These
tests assert determinism, the authorized happy path, and that each guardrail
breach produces exactly the expected closed reason code — with no AWS, runtime,
clock, or infrastructure dependency.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import load_json
from operations.contracts.autonomy import evaluate_autonomy_policy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


_AUTHORITY = {
    "deployment_mode": "operate",
    "tenant_policy": "operate",
    "workspace_policy": "operate",
    "principal_authority": "operate",
    "capability_maximum": "operate",
    "operation_risk_policy": "operate",
}

_PRINCIPAL = {
    "source_type": "automation",
    "subject_id": "subject.autonomy-agent",
    "client_id": "client.autonomy-runtime",
}


def _observation() -> dict[str, Any]:
    return {
        "capacity": {"desired": 0, "minimum": 0, "maximum": 1},
        "tenant_id": "tenant.default",
        "workspace_id": "workspace.default",
        "target_enrolled": True,
        "observed_at_epoch_seconds": 1_000_000,
    }


def _window() -> dict[str, Any]:
    return {
        "last_write_epoch_seconds": None,
        "writes_in_window": 0,
        "window_micro_usd": 0,
        "action_micro_usd": 100000,
        "in_flight": 0,
        "seconds_since_opposite_change": None,
        "direction_flips_in_window": 0,
    }


def _evaluate(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "policy": _policy(),
        "authority_inputs": dict(_AUTHORITY),
        "automation_principal": dict(_PRINCIPAL),
        "observation": _observation(),
        "requested": {"desired": 1, "minimum": 0, "maximum": 1},
        "window_state": _window(),
        "now_epoch_seconds": 1_000_060,
    }
    kwargs.update(overrides)
    return evaluate_autonomy_policy(**kwargs)


@pytest.mark.unit
def test_happy_path_is_authorized() -> None:
    result = _evaluate()
    assert result["decision"] == "authorized"
    assert result["reason_codes"] == ["APPROVED_AUTONOMOUS"]
    assert result["effective_authority"] == "operate"
    assert result["change"] == {"desired": 1, "minimum": 0, "maximum": 0}
    assert result["within_bounds"] is True


@pytest.mark.unit
def test_evaluation_is_deterministic() -> None:
    assert _evaluate() == _evaluate()


@pytest.mark.unit
def test_disabled_deployment_denies() -> None:
    authority = dict(_AUTHORITY, deployment_mode="disabled")
    result = _evaluate(authority_inputs=authority)
    assert result["decision"] == "denied"
    assert "DEPLOYMENT_DISABLED" in result["reason_codes"]
    assert "APPROVED_AUTONOMOUS" not in result["reason_codes"]


@pytest.mark.unit
def test_insufficient_authority_denies() -> None:
    authority = dict(_AUTHORITY, principal_authority="remediate")
    result = _evaluate(authority_inputs=authority)
    assert result["decision"] == "denied"
    assert result["reason_codes"] == ["INSUFFICIENT_AUTHORITY"]


@pytest.mark.unit
def test_wrong_automation_principal_denies() -> None:
    result = _evaluate(automation_principal=dict(_PRINCIPAL, subject_id="subject.impostor"))
    assert result["decision"] == "denied"
    assert result["reason_codes"] == ["AUTOMATION_PRINCIPAL_INVALID"]


@pytest.mark.unit
def test_non_automation_source_denies() -> None:
    result = _evaluate(automation_principal=dict(_PRINCIPAL, source_type="human"))
    assert "AUTOMATION_PRINCIPAL_INVALID" in result["reason_codes"]


@pytest.mark.unit
def test_workspace_mismatch_denies() -> None:
    observation = dict(_observation(), workspace_id="workspace.other")
    result = _evaluate(observation=observation)
    assert result["decision"] == "denied"
    assert result["reason_codes"] == ["WORKSPACE_MISMATCH"]


@pytest.mark.unit
def test_unenrolled_target_denies() -> None:
    observation = dict(_observation(), target_enrolled=False)
    result = _evaluate(observation=observation)
    assert result["reason_codes"] == ["TARGET_NOT_ENROLLED"]


@pytest.mark.unit
def test_bounds_exceeded_denies() -> None:
    # desired 2 exceeds the 0/1 ceiling and the max_step of 1.
    result = _evaluate(requested={"desired": 2, "minimum": 0, "maximum": 2})
    assert result["decision"] == "denied"
    assert "BOUNDS_EXCEEDED" in result["reason_codes"]


@pytest.mark.unit
def test_risk_ceiling_denies() -> None:
    policy = _policy()
    policy["risk"] = {"max_level": "low", "max_score": 5}
    result = _evaluate(policy=policy)
    assert result["decision"] == "denied"
    assert "RISK_LIMIT_EXCEEDED" in result["reason_codes"]


@pytest.mark.unit
def test_stale_observation_denies() -> None:
    # observed 1_000_000, max_age 120, now beyond age.
    result = _evaluate(now_epoch_seconds=1_000_600)
    assert result["reason_codes"] == ["OBSERVATION_STALE"]


@pytest.mark.unit
def test_future_skew_observation_denies() -> None:
    # observation in the future beyond max_skew (30s).
    result = _evaluate(now_epoch_seconds=999_960)
    assert "OBSERVATION_STALE" in result["reason_codes"]


@pytest.mark.unit
def test_action_budget_exceeded_denies() -> None:
    window = dict(_window(), action_micro_usd=600000)  # > max_action 500000
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["BUDGET_EXCEEDED"]


@pytest.mark.unit
def test_window_budget_exceeded_denies() -> None:
    window = dict(_window(), window_micro_usd=1_950_000, action_micro_usd=100000)  # 2.05M > 2M
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["BUDGET_EXCEEDED"]


@pytest.mark.unit
def test_cooldown_active_denies() -> None:
    window = dict(_window(), last_write_epoch_seconds=1_000_000)  # 60s ago < 300s cooldown
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["COOLDOWN_ACTIVE"]


@pytest.mark.unit
def test_frequency_exceeded_denies() -> None:
    window = dict(_window(), writes_in_window=4)  # == max 4
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["FREQUENCY_EXCEEDED"]


@pytest.mark.unit
def test_concurrency_limit_denies() -> None:
    window = dict(_window(), in_flight=1)  # == max_in_flight 1
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["CONCURRENCY_LIMIT"]


@pytest.mark.unit
def test_oscillation_opposite_change_denies() -> None:
    window = dict(_window(), seconds_since_opposite_change=100)  # < 600
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["OSCILLATION_BLOCKED"]


@pytest.mark.unit
def test_oscillation_direction_flips_denies() -> None:
    window = dict(_window(), direction_flips_in_window=2)  # > max 1
    result = _evaluate(window_state=window)
    assert result["reason_codes"] == ["OSCILLATION_BLOCKED"]


@pytest.mark.unit
def test_multiple_breaches_are_all_reported_sorted_without_authorized() -> None:
    observation = dict(_observation(), target_enrolled=False, workspace_id="workspace.other")
    window = dict(_window(), in_flight=1)
    result = _evaluate(observation=observation, window_state=window)
    assert result["decision"] == "denied"
    assert result["reason_codes"] == sorted(result["reason_codes"])
    assert {"TARGET_NOT_ENROLLED", "WORKSPACE_MISMATCH", "CONCURRENCY_LIMIT"}.issubset(result["reason_codes"])
    assert "APPROVED_AUTONOMOUS" not in result["reason_codes"]
