"""E2 live-harness bounded-transition tests (issue #414).

Before the harness runs against a live, fail-closed deployment (default bounds
0/1/1), its prepare request must be a bounded single-step transition within
[floor, ceiling] == [0, 1] with |desired-delta| <= max_step == 1, derived from
the current observed desired capacity — never the old unbounded 12/max20
request, which a fail-closed deployment would reject as out of bounds.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
import operations.validation.e2_shakedown as sd

ENDPOINT = "https://example.execute-api.us-west-2.amazonaws.com/prod"
ACCESS = "access-token-value"
APPROVER = "approver-token-value"
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _config(**overrides) -> sd.E2ShakedownConfig:
    values = {
        "endpoint": ENDPOINT,
        "access_token": ACCESS,
        "approver_token": APPROVER,
        "fleet_id": FLEET,
        "location": LOCATION,
        "observation_id": OBS_ID,
    }
    values.update(overrides)
    return sd.E2ShakedownConfig(**values)


def _prepare_body(config: sd.E2ShakedownConfig) -> dict:
    runner = sd.E2ShakedownRunner(config=config, transport=lambda *a, **k: None)
    return runner._prepare_body()


def test_prepare_request_is_bounded_within_zero_one() -> None:
    body = _prepare_body(_config())
    requested = body["proposal"]["requested"]
    for key in ("desired", "minimum", "maximum"):
        assert 0 <= requested[key] <= 1, requested


def test_prepare_request_is_never_the_unbounded_twelve_max_twenty() -> None:
    body = _prepare_body(_config())
    requested = body["proposal"]["requested"]
    assert requested != {"desired": 12, "minimum": 2, "maximum": 20}
    assert requested["maximum"] <= 1
    assert requested["desired"] <= 1


def test_transition_is_a_single_bounded_step_from_current_state() -> None:
    # Current desired 0 -> a single +1 step to 1 (within max_step == 1).
    body = _prepare_body(_config(current_desired=0))
    requested = body["proposal"]["requested"]
    assert requested["desired"] == 1
    assert abs(requested["desired"] - 0) <= 1

    # Current desired 1 -> a single -1 step to 0.
    body = _prepare_body(_config(current_desired=1))
    requested = body["proposal"]["requested"]
    assert requested["desired"] == 0
    assert abs(requested["desired"] - 1) <= 1


def test_current_desired_out_of_bounds_is_rejected() -> None:
    with pytest.raises(ValueError):
        _config(current_desired=12)
    with pytest.raises(ValueError):
        _config(current_desired=-1)
