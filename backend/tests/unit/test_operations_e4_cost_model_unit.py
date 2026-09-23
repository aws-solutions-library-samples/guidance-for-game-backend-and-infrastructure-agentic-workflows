"""Machine-checkable recompute of the OPTIONAL E4 control-plane cost model (#416).

``docs/operations-e4-cost-model.json`` is the single source of truth for the
incremental cost of the OPTIONAL, default-unprovisioned E4 operations control
plane. This test recomputes every scenario total from the model's own rates and
assumptions and asserts the recomputed totals equal the published totals, so the
JSON cannot drift from its own formulas. It also asserts the E4 runbook cites the
default-$0 claim.

The test asserts numbers only. It makes no network call and does not assert that
any operations infrastructure is deployed.
"""

# Standard library
import json
import math
import pathlib

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.fast]

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
MODEL_PATH = PROJECT_ROOT / "docs" / "operations-e4-cost-model.json"
RUNBOOK_PATH = PROJECT_ROOT / "docs" / "OPERATIONS_E4_CONTROL_PLANE.md"


@pytest.fixture(scope="module")
def model() -> dict:
    with MODEL_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def rates(model: dict) -> dict:
    return model["pricing"]["rates"]


def _round2(value: float) -> float:
    return math.floor(value * 100 + 0.5) / 100


def _idle_total(model: dict, rates: dict) -> float:
    a = model["assumptions"]["enabled_idle"]
    metrics = a["cw_custom_metrics"] * rates["cw_custom_metric_usd_per_metric_month"]
    alarms = a["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"]
    sweeper = (
        a["sweeper_invocations_per_month"] * rates["lambda_usd_per_request"]
        + a["sweeper_invocations_per_month"]
        * a["sweeper_gb_seconds_per_invocation"]
        * rates["lambda_usd_per_gb_second"]
    )
    appconfig = a["appconfig_retrievals_per_month"] * rates["appconfig_usd_per_config_received"]
    kms = a["kms_keys"] * rates["kms_usd_per_key_month"]
    secrets = a["secretsmanager_secrets"] * rates["secretsmanager_usd_per_secret_month"]
    return metrics + alarms + sweeper + appconfig + kms + secrets


def _action_total(model: dict, rates: dict, actions: int) -> float:
    per = model["assumptions"]["per_control_action"]
    api = actions * per["apigw_requests"] * (rates["apigw_http_usd_per_million_requests"] / 1_000_000)
    lam = (
        actions * per["lambda_invocations"] * rates["lambda_usd_per_request"]
        + actions * per["lambda_gb_seconds"] * rates["lambda_usd_per_gb_second"]
    )
    return api + lam


def test_default_unprovisioned_is_zero(model: dict):
    assert model["scenarios"]["default_unprovisioned"]["monthly_total_usd"] == 0.0
    assert model["assumptions"]["default_unprovisioned"]["monthly_total_usd"] == 0.0


def test_enabled_idle_total_recomputes(model: dict, rates: dict):
    recomputed = _round2(_idle_total(model, rates))
    assert recomputed == model["scenarios"]["enabled_idle"]["monthly_total_usd"], recomputed


def test_representative_total_recomputes(model: dict, rates: dict):
    actions = model["assumptions"]["representative_load"]["control_actions_per_month"]
    recomputed = _round2(_idle_total(model, rates) + _action_total(model, rates, actions))
    assert recomputed == model["scenarios"]["representative"]["monthly_total_usd"], recomputed


def test_appconfig_rate_million_is_consistent(rates: dict):
    per = rates["appconfig_usd_per_config_received"]
    per_million = rates["appconfig_usd_per_config_received_million"]
    assert _round2(per * 1_000_000) == per_million


def test_runbook_cites_zero_default():
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "$0" in text
    assert "416" in text
