"""Machine-checkable recompute of the OPTIONAL E3 execution cost model (#415).

``docs/operations-e3-cost-model.json`` is the single source of truth for the
incremental cost of the OPTIONAL, default-unprovisioned E3 operations execution
control plane. This test recomputes every scenario total from the model's own
rates and assumptions and asserts the recomputed totals equal the published
totals, so the JSON cannot drift from its own formulas. It also asserts the E3
runbook cites the default-$0 claim.

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
MODEL_PATH = PROJECT_ROOT / "docs" / "operations-e3-cost-model.json"
RUNBOOK_PATH = PROJECT_ROOT / "docs" / "OPERATIONS_E3_EXECUTION.md"

GIB = 1024**3
KIB = 1024


@pytest.fixture(scope="module")
def model() -> dict:
    with MODEL_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def rates(model: dict) -> dict:
    return model["pricing"]["rates"]


def _cents(value: float) -> float:
    return math.floor(value * 100 + 0.5) / 100


def _enabled_idle(model: dict, rates: dict) -> dict:
    a = model["assumptions"]["enabled_idle"]
    return {
        "cloudwatch_metrics": a["cw_custom_metrics"] * rates["cw_custom_metric_usd_per_metric_month"],
        "cloudwatch_alarms": a["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"],
    }


def _representative(model: dict, rates: dict) -> tuple[dict, dict]:
    per = model["assumptions"]["per_execution"]
    rep = model["assumptions"]["representative_load"]
    comp = model["assumptions"]["compute_choice"]
    n = rep["executions_per_month"]

    disp_gbs = (
        n
        * per["dispatcher_invocations"]
        * (comp["lambda_memory_mb"] / 1024)
        * comp["dispatcher_billed_seconds_per_dispatch"]
    )
    exec_gbs = (
        n
        * per["executor_invocations"]
        * (comp["lambda_memory_mb"] / 1024)
        * comp["executor_billed_seconds_per_execution"]
    )
    log_gb = n * (per["cw_log_kb_per_execution"] * KIB) / GIB

    variable = {
        "apigw_requests": n * per["apigw_requests"] * rates["apigw_http_request_usd_per_request"],
        "dispatcher_requests": n * per["dispatcher_invocations"] * rates["lambda_request_usd_per_request"],
        "executor_requests": n * per["executor_invocations"] * rates["lambda_request_usd_per_request"],
        "dispatcher_duration": disp_gbs * rates["lambda_gb_second_usd_x86"],
        "executor_duration": exec_gbs * rates["lambda_gb_second_usd_x86"],
        "sfn_transitions": n * per["sfn_state_transitions"] * rates["sfn_standard_transition_usd_per_transition"],
        "cloudwatch_log_ingest": log_gb * rates["cw_log_ingest_usd_per_gb"],
    }
    fixed = {
        "cloudwatch_metrics": rep["cw_custom_metrics"] * rates["cw_custom_metric_usd_per_metric_month"],
        "cloudwatch_alarms": rep["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"],
    }
    return variable, fixed


def test_pricing_metadata_is_explicit(model: dict) -> None:
    pricing = model["pricing"]
    assert pricing["region"] == "us-west-2"
    assert pricing["currency"] == "USD"
    assert pricing["as_of"]
    for svc in ("step_functions", "lambda", "api_gateway", "cloudwatch"):
        assert pricing["sources"][svc]["publication_date"]


def test_default_unprovisioned_is_zero(model: dict) -> None:
    scenario = model["scenarios"]["default_unprovisioned"]
    assert scenario["fixed_usd"] == 0.0
    assert scenario["variable_usd"] == 0.0
    assert scenario["total_usd"] == 0.0
    assert model["assumptions"]["default_unprovisioned"]["resources_created"] == 0


def test_step_functions_rate_is_guarded(rates: dict) -> None:
    # STANDARD is billed per state transition; guard the recorded us-west-2 rate.
    assert rates["sfn_standard_transition_usd_per_transition"] == 0.000025


def test_enabled_idle_matches_published(model: dict, rates: dict) -> None:
    computed = _enabled_idle(model, rates)
    published = model["scenarios"]["enabled_idle"]["line_items_usd"]
    assert set(computed) == set(published)
    for key, value in computed.items():
        assert round(value, 5) == pytest.approx(published[key], abs=1e-9), key
    scenario = model["scenarios"]["enabled_idle"]
    assert _cents(sum(computed.values())) == scenario["fixed_usd"]
    assert scenario["variable_usd"] == 0.0
    assert _cents(sum(computed.values())) == scenario["total_usd"]


def test_representative_load_matches_published(model: dict, rates: dict) -> None:
    variable, fixed = _representative(model, rates)
    published = model["scenarios"]["representative_load"]["line_items_usd"]
    combined = {**variable, **fixed}
    assert set(combined) == set(published)
    for key, value in combined.items():
        assert round(value, 5) == pytest.approx(published[key], abs=5e-3), key
    scenario = model["scenarios"]["representative_load"]
    assert _cents(sum(variable.values())) == scenario["variable_usd"]
    assert _cents(sum(fixed.values())) == scenario["fixed_usd"]
    assert _cents(sum(variable.values()) + sum(fixed.values())) == scenario["total_usd"]


def test_runbook_states_default_zero_cost() -> None:
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "$0" in text, "the E3 runbook must state the default deploy is $0"
    assert "operations-e3-cost-notes.md" in text or "operations-e3-cost-model" in text
