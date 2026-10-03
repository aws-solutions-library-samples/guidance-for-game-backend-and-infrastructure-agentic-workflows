"""Machine-checkable recompute of the optional E1 operations cost model (#281).

``docs/operations-cost-model.json`` is the single source of truth for the
incremental cost of the OPTIONAL, default-disabled E1 operations control plane
described by the accepted synchronous design in ADR 0005. This test recomputes
every scenario total from the model's own rates and assumptions using the
documented formulas, and asserts:

* the recomputed per-line-item and per-scenario totals equal the published
  totals in the JSON (so the JSON cannot drift from its own formulas), and
* the same headline totals appear verbatim in the public docs
  (README.md and docs/DEPLOYMENT_GUIDE.md), so the guidance cannot drift from
  the model.

The test asserts numbers only. It makes no network call and does not assert
that any operations infrastructure is deployed.
"""

# Standard library
import json
import math
import pathlib

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.localhost, pytest.mark.fast]

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
MODEL_PATH = PROJECT_ROOT / "docs" / "operations-cost-model.json"
README_PATH = PROJECT_ROOT / "README.md"
DEPLOYMENT_GUIDE_PATH = PROJECT_ROOT / "docs" / "DEPLOYMENT_GUIDE.md"

GIB = 1024**3
KIB = 1024


@pytest.fixture(scope="module")
def model() -> dict:
    with MODEL_PATH.open(encoding="utf-8") as handle:
        loaded: dict = json.load(handle)
    return loaded


@pytest.fixture(scope="module")
def rates(model: dict) -> dict:
    rate_map: dict = model["pricing"]["rates"]
    return rate_map


def _cents(value: float) -> float:
    """Round half-up to cents, matching the model's rounding policy."""
    return math.floor(value * 100 + 0.5) / 100


# --------------------------------------------------------------------------- #
# Formula recomputation
# --------------------------------------------------------------------------- #


def _enabled_idle_line_items(model: dict, rates: dict) -> dict:
    a = model["assumptions"]["enabled_idle"]
    return {
        "cloudwatch_metrics": a["cw_custom_metrics"] * rates["cw_custom_metric_usd_per_metric_month"],
        "cloudwatch_alarms": a["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"],
        "ddb_storage": a["ddb_stored_gb"] * rates["ddb_storage_usd_per_gb_month"],
        "ddb_pitr": a["ddb_stored_gb"] * rates["ddb_pitr_usd_per_gb_month"],
        "s3_storage": a["s3_stored_gb"] * rates["s3_storage_usd_per_gb_month"],
    }


def _representative_line_items(model: dict, rates: dict) -> tuple[dict, dict]:
    per = model["assumptions"]["per_observation_request"]
    rep = model["assumptions"]["representative_load"]
    compute = model["assumptions"]["compute_choice"]
    n = rep["observation_requests_per_month"]

    wru = per["ddb_write_request_units_per_request"]
    rru = (
        per["ddb_transactional_read_units_per_request"]
        + per["status_reads_per_observation"] * per["ddb_read_request_units_per_status_read"]
    )
    gb_seconds = n * (compute["lambda_memory_mb"] / 1024) * compute["lambda_billed_seconds_per_request"]
    log_gb = n * (per["cw_log_kb_per_request"] * KIB) / GIB
    dto_gb = n * (per["response_kb"] * KIB) / GIB

    variable = {
        "apigw_requests": n * per["apigw_requests"] * rates["apigw_http_request_usd_per_request"],
        "lambda_requests": n * per["lambda_invocations"] * rates["lambda_request_usd_per_request"],
        "lambda_duration": gb_seconds * rates["lambda_gb_second_usd_x86"],
        "ddb_writes": n * wru * rates["ddb_write_request_unit_usd"],
        "ddb_reads": n * rru * rates["ddb_read_request_unit_usd"],
        "s3_puts": n * per["s3_puts_per_request"] * rates["s3_put_usd_per_request"],
        "s3_gets": n * per["s3_gets_per_request"] * rates["s3_get_usd_per_request"],
        "cloudwatch_log_ingest": log_gb * rates["cw_log_ingest_usd_per_gb"],
        "xray_traces": n * per["xray_traces_per_request"] * rates["xray_trace_recorded_usd_per_trace"],
        "data_transfer_out": dto_gb * rates["data_transfer_out_usd_per_gb"],
    }
    fixed = {
        "cloudwatch_metrics": rep["cw_custom_metrics"] * rates["cw_custom_metric_usd_per_metric_month"],
        "cloudwatch_alarms": rep["cw_alarms"] * rates["cw_alarm_usd_per_alarm_month"],
        "ddb_storage": rep["ddb_stored_gb"] * rates["ddb_storage_usd_per_gb_month"],
        "ddb_pitr": rep["ddb_stored_gb"] * rates["ddb_pitr_usd_per_gb_month"],
        "s3_storage": rep["s3_stored_gb"] * rates["s3_storage_usd_per_gb_month"],
    }
    return variable, fixed


# --------------------------------------------------------------------------- #
# Rates match the cited AWS Price List us-west-2 values
# --------------------------------------------------------------------------- #


def test_rates_match_priced_us_west_2_values(rates: dict) -> None:
    """Guard the recorded us-west-2 rates against accidental edits."""
    assert rates["apigw_http_request_usd_per_request"] == 0.000001
    assert rates["lambda_request_usd_per_request"] == 0.0000002
    assert rates["lambda_gb_second_usd_x86"] == 0.0000166667
    assert rates["ddb_write_request_unit_usd"] == 0.000000625
    assert rates["ddb_read_request_unit_usd"] == 0.000000125
    assert rates["ddb_storage_usd_per_gb_month"] == 0.25
    assert rates["ddb_pitr_usd_per_gb_month"] == 0.20
    assert rates["s3_storage_usd_per_gb_month"] == 0.023
    assert rates["cw_log_ingest_usd_per_gb"] == 0.50
    assert rates["cw_custom_metric_usd_per_metric_month"] == 0.30
    assert rates["cw_alarm_usd_per_alarm_month"] == 0.10
    assert rates["xray_trace_recorded_usd_per_trace"] == 0.000005


def test_pricing_metadata_is_explicit(model: dict) -> None:
    pricing = model["pricing"]
    assert pricing["region"] == "us-west-2"
    assert pricing["currency"] == "USD"
    assert pricing["as_of"]  # non-empty pricing date
    for svc in ("api_gateway", "lambda", "dynamodb", "s3", "cloudwatch", "xray"):
        src = pricing["sources"][svc]
        assert src["url"].startswith("https://")
        assert src["publication_date"]


# --------------------------------------------------------------------------- #
# Default-disabled is exactly zero
# --------------------------------------------------------------------------- #


def test_default_disabled_is_zero(model: dict) -> None:
    scenario = model["scenarios"]["default_disabled"]
    assert scenario["fixed_usd"] == 0.0
    assert scenario["variable_usd"] == 0.0
    assert scenario["total_usd"] == 0.0
    assert model["assumptions"]["default_disabled"]["resources_created"] == 0


# --------------------------------------------------------------------------- #
# Enabled-idle recompute
# --------------------------------------------------------------------------- #


def test_enabled_idle_matches_published(model: dict, rates: dict) -> None:
    computed = _enabled_idle_line_items(model, rates)
    published = model["scenarios"]["enabled_idle"]["line_items_usd"]
    assert set(computed) == set(published)
    for key, value in computed.items():
        assert round(value, 5) == pytest.approx(published[key], abs=1e-9), key

    fixed = sum(computed.values())
    scenario = model["scenarios"]["enabled_idle"]
    assert _cents(fixed) == scenario["fixed_usd"]
    assert scenario["variable_usd"] == 0.0
    assert _cents(fixed) == scenario["total_usd"]


# --------------------------------------------------------------------------- #
# Representative-load recompute
# --------------------------------------------------------------------------- #


def test_representative_load_matches_published(model: dict, rates: dict) -> None:
    variable, fixed = _representative_line_items(model, rates)
    published = model["scenarios"]["representative_load"]["line_items_usd"]

    combined = {**variable, **fixed}
    assert set(combined) == set(published)
    for key, value in combined.items():
        assert round(value, 5) == pytest.approx(published[key], abs=5e-3), key

    scenario = model["scenarios"]["representative_load"]
    assert _cents(sum(variable.values())) == scenario["variable_usd"]
    assert _cents(sum(fixed.values())) == scenario["fixed_usd"]
    assert _cents(sum(variable.values()) + sum(fixed.values())) == scenario["total_usd"]


def test_variable_cost_scales_linearly(model: dict, rates: dict) -> None:
    """The per-request marginal cost stated in sensitivity must match."""
    variable, _ = _representative_line_items(model, rates)
    n = model["assumptions"]["representative_load"]["observation_requests_per_month"]
    per_request = sum(variable.values()) / n
    # Sensitivity note claims ~USD 0.0000349 per request.
    assert per_request == pytest.approx(3.49e-5, abs=2e-6)


# --------------------------------------------------------------------------- #
# Public docs cite the model totals verbatim (no drift)
# --------------------------------------------------------------------------- #


def test_public_docs_cite_model_totals(model: dict) -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    guide = DEPLOYMENT_GUIDE_PATH.read_text(encoding="utf-8")

    enabled_idle = f'{model["scenarios"]["enabled_idle"]["total_usd"]:.2f}'
    rep_total = f'{model["scenarios"]["representative_load"]["total_usd"]:.2f}'

    for doc_name, doc in (("README.md", readme), ("DEPLOYMENT_GUIDE.md", guide)):
        assert "operations control plane" in doc.lower(), doc_name
        # Default-disabled zero-cost claim is present.
        assert "$0" in doc, doc_name
        # Both computed headline totals appear verbatim.
        assert enabled_idle in doc, f"{doc_name} missing enabled-idle total {enabled_idle}"
        assert rep_total in doc, f"{doc_name} missing representative total {rep_total}"
