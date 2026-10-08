"""Bounded, code-owned projections for allowed Billing MCP outputs (#465).

The Cost specialist reaches AWS forecasting and optimization data through three
AWS Labs Billing MCP tools — ``cost-explorer`` (forecast operations only),
``compute-optimizer``, and ``cost-optimization`` (Cost Optimization Hub). Their
raw results are provider-controlled: they carry full ARNs (which embed the
account ID), caller account IDs, free-text resource names and tags, arbitrary
nested configuration blobs, continuation tokens, and provider-authored error
text. None of that may reach model context verbatim, and no model-authored
number may replace the deterministic owned cost report.

This module keeps the transform *in code we own*, mirroring
``agents.gamelift_projections`` and ``agents.eks_mcp_guard``:

* each ``project_*`` function copies only an explicit allowlist of operational
  fields, and every copied value is additionally validated by a field-specific
  typed grammar (identifier / region / token / name / number) that rejects
  control characters, ARN prefixes, URL schemes, any 12-digit account-identifier
  substring, and IPv4/IPv6/CIDR network coordinates — even when the value is
  short and otherwise grammar-valid;
* model-safe resource identifiers are *derived* from an ARN suffix when the raw
  ARN is unsafe, or omitted; full ARNs, account IDs, tags, and free-text names
  are never projected unless they pass a bounded grammar;
* numbers must be finite and within a bounded magnitude/precision; strings and
  collections are bounded by item/string caps and, in aggregate, by the final
  serialized-envelope byte budget;
* dispositions are kept mutually distinct via ``status`` (ok / empty / denied /
  incomplete / truncated) with independent boolean flags (``estimated``,
  ``truncated``, ``paginated``, ``partial``); typed sanitized error codes carry
  no provider text.

This module performs pure projection on an already-extracted payload mapping; it
never calls AWS and never logs. The guard (``agents.cost_mcp_guard``) owns tool
exposure, input validation, delegate streaming, error classification, logging,
and the final serialized-envelope byte budget.
"""

from __future__ import annotations

# Standard library
import ipaddress
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, cast

# Envelope schema version (code-owned, Cost-scoped).
ENVELOPE_VERSION = "cost-guard-1"

# --------------------------------------------------------------------------- #
# Deterministic bounds                                                        #
# --------------------------------------------------------------------------- #
MAX_ITEMS = 50  # per-collection item cap (recommendations / forecast periods)
MAX_STRING = 128  # per-string char cap for identifiers/tokens
MAX_RESOURCE_TYPES = 10  # per-recommendation resource-type list cap
MAX_ABS_NUMBER = 1e12  # finite numeric magnitude bound
MAX_DECIMAL_PLACES = 4  # projected numeric precision

# Disposition vocabulary (kept distinct on purpose).
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DENIED = "denied"
STATUS_INCOMPLETE = "incomplete"
STATUS_TRUNCATED = "truncated"

# Typed, sanitized error codes (no provider-authored text ever).
ERROR_ACCESS_DENIED = "access_denied"
ERROR_NOT_ENROLLED = "not_enrolled"
ERROR_NOT_FOUND = "not_found"
ERROR_THROTTLED = "throttled"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_DATA_UNAVAILABLE = "data_unavailable"
ERROR_CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
ERROR_PROVIDER_ERROR = "provider_error"
ERROR_MALFORMED_RESPONSE = "malformed_response"

# --------------------------------------------------------------------------- #
# Code-owned value validators (mirrors gamelift_projections grammar)          #
# --------------------------------------------------------------------------- #
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ACCOUNT_ID = re.compile(r"\d{12}")
_DOTTED_QUAD_TEXT = re.compile(r"\d+(?:\.\d+){3}")
_URL_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Field grammars.
_RE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,127}$")  # rec IDs, resource IDs
_RE_REGION = re.compile(r"^[a-z]{2,}(-[a-z0-9]+){1,4}$")
_RE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")  # enums, instance types, currency
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")

_IDENTIFIER = "identifier"
_REGION = "region"
_TOKEN = "token"
_DATE = "date"

_STRING_GRAMMARS: dict[str, "re.Pattern[str]"] = {
    _IDENTIFIER: _RE_IDENTIFIER,
    _REGION: _RE_REGION,
    _TOKEN: _RE_TOKEN,
    _DATE: _RE_DATE,
}


def _looks_like_network_coordinate(value: str) -> bool:
    candidate = value.split("/", 1)[0]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    for token in re.split(r"[\s,;]+", value):
        host = token.split("/", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            continue
    if _DOTTED_QUAD_TEXT.search(value):
        return True
    for token in re.split(r"[^0-9A-Fa-f:]+", value):
        if ":" in token:
            try:
                ipaddress.IPv6Address(token)
                return True
            except ValueError:
                continue
    return False


def _has_sensitive_coordinate(value: str) -> bool:
    if value.startswith("arn:"):
        return True
    if _ACCOUNT_ID.search(value):
        return True
    if _URL_SCHEME.search(value):
        return True
    if _looks_like_network_coordinate(value):
        return True
    return False


def valid_typed_string(value: Any, kind: str) -> bool:
    """Exact ``str`` matching ``kind``'s grammar and free of sensitive content."""
    if not isinstance(value, str):
        return False
    if len(value) > MAX_STRING:
        return False
    if _CONTROL_CHARS.search(value):
        return False
    if _has_sensitive_coordinate(value):
        return False
    grammar = _STRING_GRAMMARS.get(kind)
    if grammar is None:
        return False
    return grammar.match(value) is not None


def _typed_string(value: Any, kind: str) -> str | None:
    return cast(str, value) if valid_typed_string(value, kind) else None


def _bounded_number(value: Any) -> int | float | None:
    """Finite real number within magnitude bound, with bounded precision.

    ``bool`` is rejected (not a numeric metric). Floats are rounded to
    ``MAX_DECIMAL_PLACES`` so a provider cannot smuggle an unbounded-precision
    float into model context.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) <= MAX_ABS_NUMBER else None
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > MAX_ABS_NUMBER:
            return None
        return round(value, MAX_DECIMAL_PLACES)
    return None


def _money_from_string(value: Any) -> float | None:
    """Parse a provider monetary string ('123.45') into a bounded float.

    Cost Explorer forecast amounts and Compute Optimizer savings values arrive
    as strings. A non-finite, oversized, or non-numeric string is rejected.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _bounded_number(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    as_float = float(amount)
    if abs(as_float) > MAX_ABS_NUMBER:
        return None
    return round(as_float, MAX_DECIMAL_PLACES)


def _derive_resource_identifier(item: Mapping[str, Any]) -> str | None:
    """Return a model-safe resource identifier or ``None``.

    Prefers a grammar-valid ``resource_id``. Otherwise derives the LAST path
    segment of an ``resource_arn``/``*_arn`` suffix (never the account ID or
    region embedded in the ARN) and validates it against the identifier grammar.
    A segment that fails the grammar (or still carries a sensitive shape) yields
    ``None`` so the field is simply omitted.
    """
    direct = _typed_string(item.get("resource_id"), _IDENTIFIER)
    if direct is not None:
        return direct
    for key in ("resource_arn", "instance_arn", "function_arn", "volume_arn", "auto_scaling_group_arn"):
        arn = item.get(key)
        if isinstance(arn, str) and arn.startswith("arn:"):
            suffix = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            candidate = _typed_string(suffix, _IDENTIFIER)
            if candidate is not None:
                return candidate
    return None


def _savings(savings: Any) -> dict[str, Any] | None:
    """Project a Compute Optimizer savings opportunity: bounded value + currency."""
    if not isinstance(savings, Mapping):
        return None
    out: dict[str, Any] = {}
    pct = _bounded_number(savings.get("savings_percentage"))
    if pct is not None:
        out["savings_percentage"] = pct
    estimated = savings.get("estimated_monthly_savings")
    if isinstance(estimated, Mapping):
        value = _bounded_number(estimated.get("value"))
        currency = _typed_string(estimated.get("currency"), _TOKEN)
        if value is not None:
            out["estimated_monthly_savings"] = value
        if currency is not None:
            out["currency"] = currency
    return out or None


# --------------------------------------------------------------------------- #
# Forecast projections (cost-explorer getCostForecast)                        #
# --------------------------------------------------------------------------- #
def project_forecast(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a Cost Explorer forecast payload.

    Expected shape (boto3 GetCostForecast):
    ``{"Total": {"Amount": str, "Unit": str}, "ForecastResultsByTime": [...]}``.
    Returns ``None`` (malformed) when neither Total nor a results list is usable.
    Forecasts are always ``estimated``.

    A present-but-invalid ``Total`` (non-mapping, or a mapping whose ``Amount``
    cannot be parsed) or a ``ForecastResultsByTime`` that is present-but-wrong-
    typed (not a list) OR absent entirely is a malformed shape: the valid fields
    are kept but the result is marked ``incomplete`` + ``partial`` with
    ``malformed_response`` so it is never presented as a clean ``ok``. Nothing in
    the Cost Explorer forecast contract documents an absent results list as
    meaning "empty", so absence is treated as malformed rather than an
    authoritative empty forecast.
    """
    if not isinstance(data, Mapping):
        return None
    total_raw = data.get("Total")
    results_raw = data.get("ForecastResultsByTime")

    # Track malformed top-level shapes independently of per-row drops.
    shape_malformed = False

    total_present = "Total" in data
    total_is_mapping = isinstance(total_raw, Mapping)
    if total_present and not total_is_mapping:
        shape_malformed = True

    if not isinstance(results_raw, list):
        # A present-but-wrong-typed OR an absent results list is malformed: the
        # forecast contract does not document absence as an authoritative empty.
        shape_malformed = True
        # A missing/wrong results list with no usable Total is wholly malformed.
        if not total_is_mapping:
            return None
        results_raw = []

    out: dict[str, Any] = {"estimated": True}
    if total_is_mapping:
        total_map = cast(Mapping[str, Any], total_raw)
        raw_amount = total_map.get("Amount")
        amount = _money_from_string(raw_amount)
        unit = _typed_string(total_map.get("Unit"), _TOKEN)
        if amount is not None:
            out["total_amount"] = amount
        elif raw_amount is not None:
            # Total mapping present but its Amount is unparseable → malformed.
            shape_malformed = True
        if unit is not None:
            out["unit"] = unit

    periods: list[dict[str, Any]] = []
    dropped = False
    for row in results_raw:
        if not isinstance(row, Mapping):
            dropped = True
            continue
        tp = row.get("TimePeriod")
        period: dict[str, Any] = {}
        if isinstance(tp, Mapping):
            start = _typed_string(tp.get("Start"), _DATE)
            end = _typed_string(tp.get("End"), _DATE)
            if start is not None:
                period["start"] = start
            if end is not None:
                period["end"] = end
        mean = _money_from_string(row.get("MeanValue"))
        lower = _money_from_string(row.get("PredictionIntervalLowerBound"))
        upper = _money_from_string(row.get("PredictionIntervalUpperBound"))
        if mean is not None:
            period["mean"] = mean
        if lower is not None:
            period["prediction_interval_lower"] = lower
        if upper is not None:
            period["prediction_interval_upper"] = upper
        if period:
            periods.append(period)
        else:
            dropped = True

    # Keep only the first MAX_ITEMS valid periods; a cap on valid periods is a
    # local truncation independent of any malformed-row drop detected above.
    locally_capped = len(periods) > MAX_ITEMS
    periods = periods[:MAX_ITEMS]

    out["periods"] = periods
    if "total_amount" not in out and not periods and not shape_malformed:
        # Nothing usable at all → malformed, not an authoritative empty.
        return None

    # A dropped row and a malformed top-level shape both signal a partial,
    # malformed result; the cap is an independent truncation signal.
    partial_malformed = dropped or shape_malformed

    # Compute status by precedence (incomplete > truncated > ok/empty), then set
    # every applicable flag independently so a dropped+capped forecast keeps both
    # the malformed-partial signal and the truncation signal.
    if partial_malformed:
        out["status"] = STATUS_INCOMPLETE
    elif locally_capped:
        out["status"] = STATUS_TRUNCATED
    elif not periods and "total_amount" not in out:
        out["status"] = STATUS_EMPTY
    else:
        out["status"] = STATUS_OK

    if partial_malformed:
        out["partial"] = True
        out["error"] = {"code": ERROR_MALFORMED_RESPONSE}
    if locally_capped:
        out["truncated"] = True
    return out


# --------------------------------------------------------------------------- #
# Compute Optimizer rightsizing projections                                   #
# --------------------------------------------------------------------------- #
def _project_compute_optimizer_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    current = item.get("current_instance") or item.get("current_configuration") or {}
    current_map = current if isinstance(current, Mapping) else {}
    out: dict[str, Any] = {}
    resource_id = _derive_resource_identifier(item)
    if resource_id is not None:
        out["resource_id"] = resource_id
    finding = _typed_string(current_map.get("finding"), _TOKEN)
    if finding is not None:
        out["finding"] = finding
    current_type = _typed_string(
        current_map.get("instance_type") or current_map.get("instance_class") or current_map.get("volume_type"),
        _TOKEN,
    )
    if current_type is not None:
        out["current_type"] = current_type

    # Top-N recommended types + bounded savings.
    options_raw = item.get("recommendation_options")
    recommended: list[dict[str, Any]] = []
    if isinstance(options_raw, list):
        for option in options_raw[:3]:
            if not isinstance(option, Mapping):
                continue
            opt: dict[str, Any] = {}
            opt_type = _typed_string(
                option.get("instance_type") or option.get("instance_class") or option.get("volume_type"),
                _TOKEN,
            )
            if opt_type is not None:
                opt["type"] = opt_type
            risk = _typed_string(option.get("performance_risk"), _TOKEN)
            if risk is None:
                risk_num = _bounded_number(option.get("performance_risk"))
                if risk_num is not None:
                    opt["performance_risk"] = risk_num
            else:
                opt["performance_risk"] = risk
            savings = _savings(option.get("savings_opportunity"))
            if savings is not None:
                opt["savings"] = savings
            if opt:
                recommended.append(opt)
    out["recommended_options"] = recommended

    refresh = _typed_string(item.get("last_refresh_timestamp"), _DATE)
    if refresh is not None:
        out["last_refresh"] = refresh
    return out or None


def project_compute_optimizer(data: Mapping[str, Any], bound: int = MAX_ITEMS) -> dict[str, Any] | None:
    """Project a Compute Optimizer recommendations payload.

    Expected shape: ``{"recommendations": [...], "next_token": str|None}``.
    ``bound`` is the number of rows the model requested; the guard over-fetches
    by one, so more than ``bound`` valid rows signals residual data.
    """
    return _project_recommendation_collection(data, "recommendations", _project_compute_optimizer_item, bound=bound)


# --------------------------------------------------------------------------- #
# Cost Optimization Hub projections                                           #
# --------------------------------------------------------------------------- #
def _project_coh_recommendation(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    out: dict[str, Any] = {}
    rec_id = _typed_string(item.get("recommendation_id"), _IDENTIFIER)
    if rec_id is not None:
        out["recommendation_id"] = rec_id
    region = _typed_string(item.get("region"), _REGION)
    if region is not None:
        out["region"] = region
    resource_id = _derive_resource_identifier(item)
    if resource_id is not None:
        out["resource_id"] = resource_id
    for src_key, out_key in (
        ("current_resource_type", "current_resource_type"),
        ("recommended_resource_type", "recommended_resource_type"),
        ("action_type", "action_type"),
        ("implementation_effort", "implementation_effort"),
        ("source", "source"),
        ("currency_code", "currency"),
    ):
        token = _typed_string(item.get(src_key), _TOKEN)
        if token is not None:
            out[out_key] = token
    for src_key, out_key in (("restart_needed", "restart_needed"), ("rollback_possible", "rollback_possible")):
        value = item.get(src_key)
        if isinstance(value, bool):
            out[out_key] = value
    savings = _savings_scalar(item.get("estimated_monthly_savings"))
    if savings is not None:
        out["estimated_monthly_savings"] = savings
    pct = _bounded_number(item.get("estimated_savings_percentage"))
    if pct is not None:
        out["estimated_savings_percentage"] = pct
    lookback = _bounded_number(item.get("lookback_period_in_days"))
    if lookback is not None:
        out["lookback_period_in_days"] = lookback
    refresh = _typed_string(item.get("last_refresh_timestamp"), _DATE)
    if refresh is not None:
        out["last_refresh"] = refresh
    # Note: tags, resource_arn, account_id, and nested *_resource_details are
    # intentionally never projected.
    return out or None


def _savings_scalar(value: Any) -> float | None:
    """Cost Optimization Hub savings are a bare number (or numeric string)."""
    return _money_from_string(value)


def _project_coh_summary(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    out: dict[str, Any] = {}
    group = _typed_string(item.get("group"), _TOKEN)
    if group is not None:
        out["group"] = group
    savings = _savings_scalar(item.get("estimated_monthly_savings"))
    if savings is not None:
        out["estimated_monthly_savings"] = savings
    count = _bounded_number(item.get("recommendation_count"))
    if count is not None:
        out["recommendation_count"] = count
    return out or None


def project_cost_optimization_recommendations(data: Mapping[str, Any], bound: int = MAX_ITEMS) -> dict[str, Any] | None:
    """Project ``list_recommendations`` output."""
    return _project_recommendation_collection(data, "recommendations", _project_coh_recommendation, bound=bound)


def project_cost_optimization_summaries(data: Mapping[str, Any], bound: int = MAX_ITEMS) -> dict[str, Any] | None:
    """Project ``list_recommendation_summaries`` output."""
    result = _project_recommendation_collection(data, "summaries", _project_coh_summary, bound=bound)
    if result is None:
        return None
    group_by = _typed_string(data.get("group_by"), _TOKEN)
    if group_by is not None:
        result["group_by"] = group_by
    currency = _typed_string(data.get("currency_code"), _TOKEN)
    if currency is not None:
        result["currency"] = currency
    total = _savings_scalar(data.get("estimated_total_savings"))
    if total is not None:
        result["estimated_total_savings"] = total
    result["estimated"] = True
    return result


def project_cost_optimization_detail(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a single ``get_recommendation`` detail payload."""
    if not isinstance(data, Mapping):
        return None
    projected = _project_coh_recommendation(data)
    if projected is None:
        return None
    cost_lookback = _bounded_number(data.get("cost_calculation_lookback_period_in_days"))
    if cost_lookback is not None:
        projected["cost_calculation_lookback_period_in_days"] = cost_lookback
    projected["estimated"] = True
    projected["status"] = STATUS_OK
    return projected


# --------------------------------------------------------------------------- #
# Shared recommendation-collection projector                                  #
# --------------------------------------------------------------------------- #
def _project_recommendation_collection(
    data: Mapping[str, Any], key: str, project_item, bound: int = MAX_ITEMS
) -> dict[str, Any] | None:
    """Bound, project, and classify a recommendation collection.

    A missing/wrongly-typed collection is malformed (returns ``None`` so the
    guard emits a typed malformed outcome). An empty present list is ``empty``.
    A residual continuation token (``next_token``) marks the result paginated +
    partial so it never looks complete. Dropped rows mark it partial.

    ``bound`` is the number of rows the model requested. The guard over-fetches
    by one row (``bound + 1``) so the projector can detect that the provider had
    more rows than were requested even when the upstream helper paginates
    internally and returns NO continuation token (Cost Optimization Hub). When
    more than ``bound`` valid rows arrive, the extra rows are dropped and the
    result is marked ``incomplete`` + ``paginated`` + ``partial``.
    """
    if not isinstance(data, Mapping):
        return None
    raw_items = data.get(key)
    if not isinstance(raw_items, list):
        return None

    has_more = bool(data.get("next_token"))
    display_bound = max(1, min(bound, MAX_ITEMS))

    if not raw_items:
        out: dict[str, Any] = {key: [], "estimated": True}
        if has_more:
            out["status"] = STATUS_INCOMPLETE
            out["paginated"] = True
            out["partial"] = True
        else:
            out["status"] = STATUS_EMPTY
        return out

    # Project the whole raw collection so a malformed row anywhere — including
    # one past the display bound — is detected, then keep only the valid rows up
    # to the bound. This keeps the "dropped" (malformed) signal independent of
    # the "capped"/"over-fetch" (truncation) signals: a collection can be any
    # combination of them.
    projected_all = [project_item(item) for item in raw_items]
    valid = [row for row in projected_all if row]
    dropped = len(valid) != len(projected_all)

    # The guard over-fetches by one, so more valid rows than the requested bound
    # means residual data existed upstream even without a continuation token.
    helper_truncated = len(valid) > display_bound
    # A hard item cap applies on top of the per-request bound. The guard never
    # requests more than MAX_ITEMS + 1 rows (the single over-fetch probe row), so
    # a count of exactly MAX_ITEMS + 1 is just that probe row and must NOT look
    # like a genuine cap overflow — it is already surfaced as residual pagination.
    # Only MORE than MAX_ITEMS + 1 valid rows is a real item-cap truncation.
    # Measured against VALID rows only, so dropped malformed rows never fake a
    # cap.
    item_capped = len(valid) > MAX_ITEMS + 1

    projected = valid[:display_bound]

    out = {key: projected, "estimated": True}
    if not projected:
        # Items present but none projectable → malformed, not empty.
        return None

    residual = has_more or helper_truncated

    # Compute status by precedence (incomplete > truncated > ok), then set every
    # applicable flag independently so overlapping conditions are never lost. A
    # residual (continuation token OR over-fetch) means the result is genuinely
    # incomplete and outranks a local item-cap truncation.
    if dropped or residual:
        out["status"] = STATUS_INCOMPLETE
    elif item_capped:
        out["status"] = STATUS_TRUNCATED
    else:
        out["status"] = STATUS_OK

    if dropped:
        out["partial"] = True
        out["error"] = {"code": ERROR_MALFORMED_RESPONSE}
    if item_capped:
        out["truncated"] = True
    if residual:
        out["paginated"] = True
        out["partial"] = True
    return out
