"""Machine-checkable recompute of the OPTIONAL E5 autonomy cost model (#440).

``docs/operations-e5-cost-model.json`` is the single source of truth for the
incremental cost of the OPTIONAL, default-unprovisioned E5 bounded-autonomy
control plane. This test recomputes every scenario total from the model's own
rates and assumptions and asserts the recomputed totals equal the published
totals, so the JSON cannot drift from its own formulas. It also reconciles the
model's alarm count against the 09 template and asserts the notes cite the
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

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = [pytest.mark.unit, pytest.mark.fast]

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
MODEL_PATH = PROJECT_ROOT / "docs" / "operations-e5-cost-model.json"
NOTES_PATH = PROJECT_ROOT / "docs" / "operations-e5-cost-notes.md"
TEMPLATE = PROJECT_ROOT / "infrastructure" / "cloudformation" / "09-operations-autonomy.yaml"


@pytest.fixture(scope="module")
def model() -> dict:
    with MODEL_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def rates(model: dict) -> dict:
    return model["pricing"]["rates"]


def _round2(value: float) -> float:
    return math.floor(value * 100 + 0.5) / 100


def _disabled_total(model: dict, rates: dict) -> float:
    a = model["assumptions"]["provisioned_disabled"]
    return a["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"]


def _idle_total(model: dict, rates: dict) -> float:
    a = model["assumptions"]["enabled_idle"]
    alarms = a["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"]
    lam = (
        a["evaluator_invocations_per_month"] * rates["lambda_usd_per_request"]
        + a["evaluator_invocations_per_month"]
        * a["evaluator_gb_seconds_per_invocation"]
        * rates["lambda_usd_per_gb_second"]
    )
    appconfig = a["appconfig_retrievals_per_month"] * rates["appconfig_usd_per_config_received"]
    return alarms + lam + appconfig


def test_default_unprovisioned_is_zero(model: dict):
    assert model["scenarios"]["default_unprovisioned"]["monthly_total_usd"] == 0.0
    assert model["assumptions"]["default_unprovisioned"]["monthly_total_usd"] == 0.0


def test_provisioned_disabled_total_recomputes(model: dict, rates: dict):
    recomputed = _round2(_disabled_total(model, rates))
    assert recomputed == model["scenarios"]["provisioned_disabled"]["monthly_total_usd"], recomputed


def test_enabled_idle_total_recomputes(model: dict, rates: dict):
    recomputed = _round2(_idle_total(model, rates))
    assert recomputed == model["scenarios"]["enabled_idle"]["monthly_total_usd"], recomputed


def test_representative_equals_idle(model: dict):
    assert (
        model["scenarios"]["representative"]["monthly_total_usd"]
        == model["scenarios"]["enabled_idle"]["monthly_total_usd"]
    )


def test_alarm_count_reconciles_with_template(model: dict):
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    alarms = [name for name, body in template["Resources"].items() if body.get("Type") == "AWS::CloudWatch::Alarm"]
    assert len(alarms) == len(model["alarms"]["e5_owned"])
    assert model["assumptions"]["enabled_idle"]["cw_alarms"] == len(alarms)


def test_no_custom_metrics_claimed(model: dict):
    assert model["custom_metrics"]["e5_autonomy"] == []


def test_notes_cite_default_zero():
    text = NOTES_PATH.read_text(encoding="utf-8")
    assert "$0" in text
