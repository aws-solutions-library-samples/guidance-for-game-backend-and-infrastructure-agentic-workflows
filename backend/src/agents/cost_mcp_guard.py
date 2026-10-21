"""Cost-owned safety boundary for the Billing MCP tool output (#465).

The Cost specialist consumes the AWS Labs ``billing-cost-management-mcp-server``
as a Strands tool provider. That package (0.0.29) registers ~35 tools, many of
which return actual historical spend (budgets, anomalies, free tier, invoicing,
cost comparison, RI/SP performance, billing-group cost reports) and would bypass
the deterministic owned cost report, or return raw provider JSON with ARNs,
account IDs, tags, and arbitrary nested blobs plus provider-authored error text.

At the model-facing tool boundary this module:

  1. Enforces an explicit reviewed **tool allowlist** — only ``cost-explorer``,
     ``compute-optimizer``, and ``cost-optimization`` are ever model-visible.
     Every other current tool AND any tool a future package version adds is
     dropped (never advertised, never invokable).
  2. Constrains each exposed tool's ``operation`` to the reviewed set and
     guards each tool's **input** before dispatch with per-operation key
     allowlists and bounds (rejects ``account_ids``/cross-account params,
     ``group_by=AccountId``, unbounded filters/strings, out-of-range
     ``max_results``, malformed dates). ``getCostAndUsage`` and
     ``getCostAndUsageWithResources`` stay blocked with guidance to use
     ``get_cost_report``.
  3. Streams the delegate under a bounded deadline so a hung provider yields a
     typed outcome.
  4. Projects allowed output onto Cost-owned per-field grammars with finite
     bounded numbers, item/string caps, and a versioned code-owned envelope
     (see ``agents.cost_projections``), measured against the FINAL serialized
     byte budget.
  5. Replaces provider-authored error bodies with sanitized typed outcomes;
     access-denied and enrollment errors stay distinguishable. Logs carry only a
     code-owned operation name, a typed code, and a bounded correlation token.

This is deliberately Cost-owned and self-contained. It preserves the MCP client
lifecycle (add/remove consumer delegation) and the deterministic owned cost
report path, which it never touches.
"""

from __future__ import annotations

# Standard library
import asyncio
import json
import re
import uuid
from typing import Any, Callable, Mapping, Sequence, cast

# Third-party packages
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolResult, ToolSpec, ToolUse

# Local modules
from agents.cost_projections import (
    ENVELOPE_VERSION,
    ERROR_ACCESS_DENIED,
    ERROR_CREDENTIALS_UNAVAILABLE,
    ERROR_DATA_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_MALFORMED_RESPONSE,
    ERROR_NOT_ENROLLED,
    ERROR_NOT_FOUND,
    ERROR_PROVIDER_ERROR,
    ERROR_THROTTLED,
    STATUS_DENIED,
    STATUS_EMPTY,
    STATUS_INCOMPLETE,
    STATUS_OK,
    STATUS_TRUNCATED,
    project_compute_optimizer,
    project_cost_optimization_detail,
    project_cost_optimization_recommendations,
    project_cost_optimization_summaries,
    project_forecast,
    valid_typed_string,
)
from config import settings
from utils.logger import logger

_BILLING_MCP_SERVER = "billing-cost-management-mcp-server"

_COST_EXPLORER_TOOL = "cost-explorer"
_COMPUTE_OPTIMIZER_TOOL = "compute-optimizer"
_COST_OPTIMIZATION_TOOL = "cost-optimization"

# The ONLY model-visible Billing MCP tools after this guard: cost-explorer
# (forecast operations only), compute-optimizer, and cost-optimization.
_ALLOWED_TOOLS = frozenset({_COST_EXPLORER_TOOL, _COMPUTE_OPTIMIZER_TOOL, _COST_OPTIMIZATION_TOOL})

MAX_ENVELOPE_BYTES = 20_000
DEADLINE_SECONDS = 25.0

# Fixed, code-owned messages. Provider bodies never appear here.
_MSG_BLOCKED_HISTORICAL = (
    "This historical Cost Explorer operation is disabled. Use get_cost_report so totals, "
    "rankings, and percentages come from one validated snapshot."
)
_MSG_DENIED_INPUT = "This request was rejected because an argument is not permitted or out of bounds."
_MSG_DENIED_OP = "This Billing operation is not available through this assistant."
# Used for the bounded-deadline / delegate-exception outcome, where retrying a
# narrower request can genuinely help. Not reused for arbitrary provider errors.
_MSG_UNAVAILABLE = "The Billing request could not be completed. Try a narrower request."
# A cause-neutral message for an unclassified provider error. The outcomes that
# land here (internal errors, service unavailability, billing-view health,
# connection failures) are not helped by narrowing the request, so this carries
# no cause-specific advice.
_MSG_PROVIDER_ERROR = "The Billing service could not complete this request. Try again later."
_MSG_MALFORMED = "The Billing response could not be interpreted safely."

# Per-typed-code guidance so an incomplete outcome tells the model what to do.
# Each message is code-owned and carries no provider text. ``_MSG_PROVIDER_ERROR``
# is the cause-neutral fallback for an unmapped provider error; the bounded-
# deadline / delegate-exception outcome keeps ``_MSG_UNAVAILABLE`` separately.
_MSG_FOR_CODE: dict[str, str] = {
    ERROR_THROTTLED: "The Billing service is rate-limiting this request. Wait a moment and retry.",
    ERROR_NOT_FOUND: "The requested Billing resource was not found.",
    ERROR_NOT_ENROLLED: "This Billing feature is not enabled for this account.",
    ERROR_DATA_UNAVAILABLE: "Billing has no data for the requested scope or period yet. Try a different scope.",
    ERROR_CREDENTIALS_UNAVAILABLE: "Billing access is unavailable for this request.",
    ERROR_INVALID_REQUEST: "This Billing request was rejected as invalid. Adjust the parameters and retry.",
    ERROR_PROVIDER_ERROR: _MSG_PROVIDER_ERROR,
}


def _message_for_code(code: str) -> str:
    """Return the code-owned guidance message for a typed error code."""
    return _MSG_FOR_CODE.get(code, _MSG_PROVIDER_ERROR)


_CODE_DENIED = "denied"
_CODE_DENIED_INPUT = "denied_input"
_CODE_UNAVAILABLE = "unavailable"
_CODE_MALFORMED = "malformed"

# Operation names safe to echo verbatim in an envelope: the reviewed and the
# explicitly blocked historical operations. Any other (model-authored) value is
# replaced with this bounded code-owned token so an arbitrary string can never
# inflate an envelope or reach model context.
_OTHER_OP = "other"
_ECHOABLE_OPS = frozenset(
    {"getCostForecast", "getCostAndUsage", "getCostAndUsageWithResources"}
    | {"get_ec2_instance_recommendations", "get_auto_scaling_group_recommendations"}
    | {"list_recommendation_summaries", "list_recommendations", "get_recommendation"}
)


def _echo_op(operation: Any) -> str:
    """Echo an allowlisted/blocked operation name verbatim, else a fixed token."""
    return operation if isinstance(operation, str) and operation in _ECHOABLE_OPS else _OTHER_OP


# --------------------------------------------------------------------------- #
# Reviewed operation allowlists per tool                                      #
# --------------------------------------------------------------------------- #
# Only cost forecasting is reviewed on cost-explorer. No base policy granted
# ``ce:GetUsageForecast``, so getUsageForecast never worked; it is off the
# surface.
_CE_FORECAST_OPS = frozenset({"getCostForecast"})
# Blocked historical ops get a specific get_cost_report redirect (matched
# case-insensitively).
_CE_BLOCKED_OPS = frozenset({"getCostAndUsage", "getCostAndUsageWithResources"})
_CE_BLOCKED_OPS_LOWER = frozenset(op.lower() for op in _CE_BLOCKED_OPS)

# Reviewed Compute Optimizer ops: EC2 and Auto Scaling rightsizing only. EBS and
# Lambda rightsizing depend on IAM actions (ec2:DescribeVolumes,
# lambda:ListFunctions, lambda:ListProvisionedConcurrencyConfigs) that are not
# granted to the chat runtime, so they never returned data; they are left off
# the model-visible surface rather than expanding provider capability.
_CO_OPS = frozenset({"get_ec2_instance_recommendations", "get_auto_scaling_group_recommendations"})
_COH_OPS = frozenset({"list_recommendation_summaries", "list_recommendations", "get_recommendation"})

# Cost Optimization Hub group_by values; AccountId is explicitly excluded.
_COH_GROUP_BY = frozenset(
    {"Region", "ActionType", "ResourceType", "RestartNeeded", "RollbackPossible", "ImplementationEffort"}
)

# The upstream Cost Optimization Hub get_recommendation tool requires a
# resource_type argument for a presence check only (its helper documents it as
# unused by the API call). The guard supplies this fixed, non-sensitive
# placeholder so the model only needs to pass the recommendation_id.
_COH_RECOMMENDATION_RESOURCE_TYPE = "recommendation"

# Reviewed forecast metric / granularity enums. Only cost forecasting remains.
_CE_COST_METRICS = frozenset(
    {"UNBLENDED_COST", "BLENDED_COST", "AMORTIZED_COST", "NET_UNBLENDED_COST", "NET_AMORTIZED_COST"}
)
_CE_GRANULARITY = frozenset({"DAILY", "MONTHLY"})

# Cost Explorer forecast horizon per granularity (public API limits): up to 3
# months of DAILY or 18 months of MONTHLY forecasts.
_MAX_FORECAST_DAYS_DAILY = 93  # ~3 months
_MAX_FORECAST_DAYS_MONTHLY = 550  # ~18 months

# Code-owned forecast scope: SERVICE names and REGION codes the model may send
# as flat lists. The guard builds the canonical Cost Explorer Dimensions
# expression itself; the model never supplies raw filter JSON.
_MAX_SCOPE_VALUES = 10
# SERVICE display names use letters, digits, spaces, and the punctuation real
# AWS service names contain (parentheses, hyphen, period, ampersand, slash,
# comma, apostrophe). Anchored, bounded, and still subject to the shared
# sensitive-shape checks below.
_RE_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()\-.,&/']{0,126}$")
_RE_REGION_CODE = re.compile(r"^[a-z]{2,}(-[a-z0-9]+){1,4}$")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ACCOUNT_ID_RE = re.compile(r"\d{12}")

# Cross-account / unsafe keys that must never be forwarded on any tool.
_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "account_ids",
        "accountIds",
        "account_id",
        "billing_view_arn",
        "next_token",
        "nextToken",
        "max_pages",
        "filter",
        "filters",
    }
)

MAX_RESULTS_MIN, MAX_RESULTS_CAP, MAX_RESULTS_DEFAULT = 1, 50, 25
_MAX_STR_INPUT = 128


def _log_outcome(operation: str, code: str, cid: str) -> None:
    """Log a bounded operation token, a fixed typed code, and a per-invocation
    correlation token (no provider text, identifiers, or inputs)."""
    op = operation if operation in (_ALLOWED_TOOLS) else "other"
    logger.info(f"cost_guard tool={op} code={code} cid={cid}")


def _dumps(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


_ERROR_CODE_FOR_OUTCOME = {
    STATUS_DENIED: ERROR_ACCESS_DENIED,
}


def _sanitized_error(tool_use_id: str, operation: str, status: str, code: str, message: str) -> ToolResult:
    body: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "operation": _echo_op(operation),
        "status": status,
        "error": {"code": code},
        "message": message,
    }
    return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": _dumps(body)}]}


def _trim_largest_collection(body: dict[str, Any]) -> bool:
    """Shrink the largest list to fit the aggregate byte budget.

    This is a LOCAL byte trim, not provider continuation. It sets ``truncated``
    and only promotes an ``ok``/``empty`` status to ``truncated``; it never
    overwrites ``incomplete``/``denied`` or an existing ``error``, and never
    sets ``paginated`` (reserved for residual provider continuation).
    """
    best_key, best_len = None, 0
    for key, val in body.items():
        if isinstance(val, list) and len(val) > best_len:
            best_key, best_len = key, len(val)
    if best_key is None or best_len == 0:
        return False
    body[best_key] = body[best_key][: max(0, best_len // 2)]
    body["truncated"] = True
    if body.get("status") in (STATUS_OK, STATUS_EMPTY, None):
        body["status"] = STATUS_TRUNCATED
    return True


def _ok(tool_use_id: str, operation: str, projected: dict[str, Any], scope: Any = None) -> ToolResult:
    """Emit one model-visible JSON content block, capped by final UTF-8 bytes."""
    envelope: dict[str, Any] = {"v": ENVELOPE_VERSION, "operation": _echo_op(operation), **projected}
    if scope is not None:
        envelope["scope"] = scope
    for _ in range(24):
        text = _dumps(envelope)
        if len(text.encode("utf-8")) <= MAX_ENVELOPE_BYTES:
            return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}
        if not _trim_largest_collection(envelope):
            break
    # Withheld fallback: aggregate trim could not fit the budget. Mark locally
    # truncated but preserve any prior non-ok status and error code, and do not
    # claim provider continuation (paginated) as a trim artifact.
    fallback: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "operation": _echo_op(operation),
        "status": projected.get("status", STATUS_TRUNCATED) or STATUS_TRUNCATED,
        "truncated": True,
        "message": "response exceeded the maximum size and was withheld",
    }
    if fallback["status"] in (STATUS_OK, STATUS_EMPTY):
        fallback["status"] = STATUS_TRUNCATED
    for flag in ("paginated", "partial"):
        if projected.get(flag) is True:
            fallback[flag] = True
    if isinstance(projected.get("error"), Mapping):
        fallback["error"] = {"code": projected["error"].get("code")}
    return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": _dumps(fallback)}]}


# --------------------------------------------------------------------------- #
# Payload extraction (FastMCP/Strands wire shape)                             #
# --------------------------------------------------------------------------- #
def _extract_payload(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the provider ``data`` mapping from a Strands ToolResult.

    FastMCP serializes the billing tools' ``format_response`` dict into
    ``structuredContent={"status","data",...}`` plus one JSON text block with
    the same body. Prefer structuredContent; fall back to the first JSON text
    block that is a mapping. The projected payload is the ``data`` sub-mapping.
    """
    sc = result.get("structuredContent")
    body: Mapping[str, Any] | None = sc if isinstance(sc, Mapping) else None
    if body is None:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    try:
                        decoded = json.loads(item["text"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(decoded, Mapping):
                        body = decoded
                        break
    if body is None:
        return None
    data = body.get("data")
    return data if isinstance(data, Mapping) else None


def _result_signals_error(result: Mapping[str, Any]) -> bool:
    """A provider error is signalled by isError, status=='error', or a body status."""
    if result.get("status") == "error" or result.get("isError") is True:
        return True
    sc = result.get("structuredContent")
    if isinstance(sc, Mapping) and sc.get("status") in ("error", "warning"):
        return True
    return False


# Bounded denial/enrollment/throttle markers (fixed prefix scan; text discarded).
_CLASSIFY_PREFIX = 512
_DENIED_MARKERS = ("accessdenied", "not authorized", "unauthorized", "forbidden")
_ENROLL_MARKERS = ("not active", "opt-in required", "optinrequired", "enrollment", "not enabled", "not enrolled")
_THROTTLE_MARKERS = ("throttl", "too many requests", "rate exceeded")
_INVALID_MARKERS = ("validation", "invalid")

# Structured provider error codes -> (status, typed code). Checked before any
# free-text marker scan so a denial that merely mentions enrollment (or vice
# versa) is classified from the authoritative code, not incidental wording.
_ACCESS_DENIED_CODES = frozenset({"accessdeniedexception", "accessdenied", "unauthorizedoperation"})
_NOT_ENROLLED_CODES = frozenset({"optinrequiredexception", "computeoptimizernotactive"})
_THROTTLED_CODES = frozenset(
    {"throttlingexception", "throttling", "toomanyrequestsexception", "limitexceededexception"}
)
_INVALID_CODES = frozenset({"validationexception", "invalidparametervalueexception"})
_NOT_FOUND_CODES = frozenset({"resourcenotfoundexception", "notfoundexception"})
_DATA_UNAVAILABLE_CODES = frozenset({"dataunavailableexception"})
# Credential / authentication failures that are not an authorization denial of a
# specific resource. Kept distinct from ``access_denied`` so the model learns the
# request could not be authenticated rather than that it lacked permission.
_CREDENTIALS_CODES = frozenset(
    {
        "unrecognizedclientexception",
        "expiredtokenexception",
        "missingauthenticationtoken",
        "missingauthenticationtokenexception",
        "incompletesignatureexception",
    }
)

# botocore ``BotoCoreError`` subclasses that mean credentials could not be
# obtained. The Billing MCP ``handle_aws_error`` reports a ``BotoCoreError`` with
# top-level ``error_type == "aws_connection_error"`` and the exception class name
# in ``boto_error_type``; only these specific classes are a credential failure,
# while other connection errors (e.g. ``EndpointConnectionError``) stay a generic
# provider error. The class name is matched but never returned or logged.
_BOTO_CREDENTIAL_CLASSES = frozenset({"nocredentialserror", "partialcredentialserror", "credentialretrievalerror"})
_AWS_CONNECTION_ERROR_TYPE = "aws_connection_error"
# Fixed botocore ``NoCredentialsError`` text (lowercased). The Cost Optimization
# Hub list dispatcher catches that ``BotoCoreError`` in its generic handler and
# places ``str(exc)`` (this exact string, with no "An error occurred (...)"
# prefix) in a ``service_error`` ``data.message``. Matched case-insensitively
# after trimming, consistent with the class-name match; the message is never
# returned or logged.
_NO_CREDENTIALS_TEXT = "unable to locate credentials"
# Fixed botocore ``CredentialRetrievalError`` message prefix (lowercased).
# ``str(CredentialRetrievalError(...))`` begins with this literal before the
# provider name, so a credential-retrieval failure re-raised into a Hub
# ``service_error`` ``data.message`` has no "An error occurred (...)" code token.
# Matched as a case-insensitive prefix after trimming; the message is never
# returned or logged.
_CREDENTIAL_RETRIEVAL_PREFIX = "error when retrieving credentials from "
# Synthetic lowercase token appended when any credential shape is detected, so
# the existing ``_CREDENTIALS_CODES`` classification path handles it uniformly.
_CREDENTIALS_TOKEN = "credentials_error"

# A re-raised botocore ClientError string begins with "An error occurred (<Code>)".
# The Cost Optimization Hub list dispatcher embeds that string in
# ``data.message`` under the fixed ``error_type == "service_error"``. Extract ONLY
# the bounded, code-shaped token in the parentheses; the surrounding text (which
# can carry provider identifiers) is never returned or logged.
_BOTOCORE_CODE_RE = re.compile(r"An error occurred \(([A-Za-z]{3,64})\)")
# Fixed sentinel for the Cost Explorer client-creation failure body. The
# installed Cost Explorer tool creates its boto3 client in its own ``try`` and
# reports a failure as ``data.error_type == "client_creation_error"`` with
# ``repr(exc)`` in ``data.details`` (and NO top-level ``error_type`` or
# ``boto_error_type``). ``repr()`` of a botocore exception starts with the class
# name followed by ``(``; extract ONLY that anchored, bounded class token so a
# credential-resolution failure classifies as a credential error. The details
# text is read only for this token and is never returned or logged.
_CLIENT_CREATION_ERROR_TYPE = "client_creation_error"
_REPR_CLASS_RE = re.compile(r"^\s*([A-Za-z]{3,64})\(")


def _structured_error_codes(result: Mapping[str, Any]) -> list[str]:
    """Collect lowercase provider error codes from the authoritative structured
    body. Billing MCP exposes them in several shapes: under
    ``structuredContent.data`` (Compute Optimizer / Cost Optimization Hub
    ``aws_error_code``/``error_code``), at the TOP level of the body (Cost
    Explorer ``handle_aws_error`` sets ``error_type`` with no ``data`` key), and
    — for the Cost Optimization Hub list dispatcher — as the fixed sentinel
    ``data.error_type == "service_error"`` whose real botocore code lives only in
    ``data.message``. For that one shape the parenthesized code token is parsed
    from the botocore prefix; the message text itself is never read beyond that
    bounded token.

    Credential-failure shapes are also recognized and reduced to a synthetic
    credential token across every path a credential failure can surface: a
    top-level ``aws_connection_error`` whose ``boto_error_type`` is a known
    botocore credential class; a Cost Explorer ``data.error_type ==
    "client_creation_error"`` whose ``data.details`` ``repr()`` names a known
    credential class; and a Hub ``service_error`` whose ``data.message`` is
    botocore's fixed "unable to locate credentials" text or starts with the
    "error when retrieving credentials from " prefix (both matched
    case-insensitively, and neither carrying a code token). The class name,
    details, and message are read only for comparison and are never returned or
    logged.

    Only authoritative code fields are read — never free text that could echo
    model input."""
    codes: list[str] = []
    sc = result.get("structuredContent")
    if isinstance(sc, Mapping):
        for key in ("aws_error_code", "error_code", "error_type"):
            val = sc.get(key)
            if isinstance(val, str):
                codes.append(val.strip().lower())
        # A BotoCoreError credential failure arrives as a top-level
        # aws_connection_error whose boto_error_type names the exception class.
        # Only the known credential classes map to a credential failure; the
        # class name is read for comparison and never returned or logged.
        top_error_type = sc.get("error_type")
        if isinstance(top_error_type, str) and top_error_type.strip().lower() == _AWS_CONNECTION_ERROR_TYPE:
            boto_type = sc.get("boto_error_type")
            if isinstance(boto_type, str) and boto_type.strip().lower() in _BOTO_CREDENTIAL_CLASSES:
                codes.append(_CREDENTIALS_TOKEN)
        data = sc.get("data")
        if isinstance(data, Mapping):
            data_error_type = data.get("error_type")
            for key in ("aws_error_code", "error_code", "error_type"):
                val = data.get(key)
                if isinstance(val, str):
                    codes.append(val.strip().lower())
            # The Cost Explorer tool creates its boto3 client in its own try and
            # reports a client-creation failure as client_creation_error with
            # repr(exc) in data.details (no top-level error_type/boto_error_type).
            # A credential-resolution failure raised WHILE CREATING the Cost
            # Explorer client (for example PartialCredentialsError, resolved only
            # at client creation) surfaces only in this shape. The same classes
            # can also surface elsewhere: NoCredentialsError raised at signing and
            # a refresh-time CredentialRetrievalError arrive as aws_connection_error
            # (handled above). Read the anchored class token from repr() and map
            # it; the details text is never returned or logged.
            if isinstance(data_error_type, str) and data_error_type.strip().lower() == _CLIENT_CREATION_ERROR_TYPE:
                details = data.get("details")
                if isinstance(details, str):
                    cls = _REPR_CLASS_RE.match(details)
                    if cls and cls.group(1).lower() in _BOTO_CREDENTIAL_CLASSES:
                        codes.append(_CREDENTIALS_TOKEN)
            # The CoH list dispatcher sets the fixed sentinel "service_error" and
            # keeps the real botocore code only inside data.message. Extract the
            # bounded code-shaped token so throttling/internal errors classify
            # correctly without echoing the provider text.
            if isinstance(data_error_type, str) and data_error_type.strip().lower() == "service_error":
                msg = data.get("message")
                if isinstance(msg, str):
                    match = _BOTOCORE_CODE_RE.match(msg)
                    low = msg.strip().lower()
                    if match:
                        codes.append(match.group(1).strip().lower())
                    # A re-raised credential failure carries no "An error
                    # occurred (...)" prefix, only botocore's fixed text:
                    # NoCredentialsError -> exactly "unable to locate
                    # credentials"; CredentialRetrievalError -> a message
                    # starting "error when retrieving credentials from ".
                    # Matched case-insensitively after trimming so a credential
                    # failure on the Hub list path classifies as a credential
                    # error rather than a generic provider error. The text itself
                    # is never returned or logged.
                    elif low == _NO_CREDENTIALS_TEXT or low.startswith(_CREDENTIAL_RETRIEVAL_PREFIX):
                        codes.append(_CREDENTIALS_TOKEN)
    return codes


def _classify_error(result: Mapping[str, Any]) -> tuple[str, str, str]:
    """Classify a provider error WITHOUT returning or logging its raw body.

    Returns (status, error_code, message). Structured provider error codes are
    authoritative and checked first (the top-level ``error_type``, any ``data.*``
    codes, and the bounded botocore code token parsed from a ``service_error``
    ``data.message``). A structured code therefore takes precedence over any
    free-text scan. Only if no structured code matches is a bounded prefix of the
    provider-authored top-level message scanned, with denial taking precedence
    over enrollment. Model-authored input (echoed resource ids / messages under a
    ``data`` sub-mapping) is NOT part of the scanned text. The scanned text is
    never returned or logged. Each typed code carries its own code-owned message.
    """
    codes = _structured_error_codes(result)

    for code in codes:
        if code in _ACCESS_DENIED_CODES or code == "access_denied":
            return STATUS_DENIED, ERROR_ACCESS_DENIED, _MSG_DENIED_OP
        if code in _CREDENTIALS_CODES or code == "credentials_error":
            return STATUS_INCOMPLETE, ERROR_CREDENTIALS_UNAVAILABLE, _message_for_code(ERROR_CREDENTIALS_UNAVAILABLE)
        if code in _NOT_ENROLLED_CODES or code in ("enrollment_error", "opt_in_required"):
            return STATUS_INCOMPLETE, ERROR_NOT_ENROLLED, _message_for_code(ERROR_NOT_ENROLLED)
        if code in _NOT_FOUND_CODES or code == "not_found":
            return STATUS_INCOMPLETE, ERROR_NOT_FOUND, _message_for_code(ERROR_NOT_FOUND)
        if code in _THROTTLED_CODES or code == "throttling_error":
            return STATUS_INCOMPLETE, ERROR_THROTTLED, _message_for_code(ERROR_THROTTLED)
        if code in _DATA_UNAVAILABLE_CODES or code == "data_unavailable":
            return STATUS_INCOMPLETE, ERROR_DATA_UNAVAILABLE, _message_for_code(ERROR_DATA_UNAVAILABLE)
        if code in _INVALID_CODES or code == "validation_error":
            return STATUS_INCOMPLETE, ERROR_INVALID_REQUEST, _message_for_code(ERROR_INVALID_REQUEST)

    # Fallback: bounded free-text marker scan (denial before enrollment). Only
    # the provider-authored top-level message is scanned, never a ``data`` sub-
    # mapping that may echo the model's own resource_id / resource_type input.
    haystack = ""
    sc = result.get("structuredContent")
    if isinstance(sc, Mapping):
        msg = sc.get("message")
        if isinstance(msg, str):
            haystack += " " + msg[:_CLASSIFY_PREFIX]
    low = haystack.lower()
    if any(m in low for m in _DENIED_MARKERS):
        return STATUS_DENIED, ERROR_ACCESS_DENIED, _MSG_DENIED_OP
    if any(m in low for m in _ENROLL_MARKERS):
        return STATUS_INCOMPLETE, ERROR_NOT_ENROLLED, _message_for_code(ERROR_NOT_ENROLLED)
    if any(m in low for m in _THROTTLE_MARKERS):
        return STATUS_INCOMPLETE, ERROR_THROTTLED, _message_for_code(ERROR_THROTTLED)
    if any(m in low for m in _INVALID_MARKERS):
        return STATUS_INCOMPLETE, ERROR_INVALID_REQUEST, _message_for_code(ERROR_INVALID_REQUEST)
    return STATUS_INCOMPLETE, ERROR_PROVIDER_ERROR, _message_for_code(ERROR_PROVIDER_ERROR)


# --------------------------------------------------------------------------- #
# Input validation + canonical delegate input per operation                   #
# --------------------------------------------------------------------------- #
def _bounded_max_results(inp: Mapping[str, Any]) -> tuple[bool, int]:
    if "max_results" not in inp:
        return True, MAX_RESULTS_DEFAULT
    raw = inp.get("max_results")
    # Reject non-int, bool, or anything below the schema minimum (1). 0 is
    # rejected as denied_input so behavior matches the advertised minimum.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < MAX_RESULTS_MIN:
        return False, MAX_RESULTS_DEFAULT
    return True, max(MAX_RESULTS_MIN, min(raw, MAX_RESULTS_CAP))


# Per-operation ALLOWED input keys. Any key outside the set for the resolved
# operation is a denied_input rejection (never silently dropped).
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "getCostForecast": frozenset(
        {
            "operation",
            "metric",
            "granularity",
            "start_date",
            "end_date",
            "prediction_interval_level",
            "services",
            "regions",
        }
    ),
    "get_ec2_instance_recommendations": frozenset({"operation", "region", "max_results"}),
    "get_auto_scaling_group_recommendations": frozenset({"operation", "region", "max_results"}),
    "list_recommendations": frozenset({"operation", "max_results"}),
    "list_recommendation_summaries": frozenset({"operation", "group_by", "max_results"}),
    "get_recommendation": frozenset({"operation", "resource_id"}),
}


def _reject_unknown_keys(operation: str, inp: Mapping[str, Any]) -> bool:
    """True when every input key is allowed for ``operation``."""
    allowed = _ALLOWED_KEYS.get(operation)
    if allowed is None:
        return False
    return all(key in allowed for key in inp)


def _valid_scope_values(raw: Any, kind: str) -> list[str] | None:
    """Validate a flat forecast-scope list (SERVICE names or REGION codes).

    1-10 values, each at most ``_MAX_STR_INPUT`` characters (the advertised
    ``maxLength``), matching the per-kind grammar in full (``fullmatch`` so a
    trailing newline the ``$`` anchor would otherwise allow is rejected) and
    passing the shared sensitive-shape checks (no control chars, ARN prefix, URL
    scheme, 12-digit run, or network coordinate). Duplicates are rejected.
    Returns the values sorted into a canonical order (so two inputs that differ
    only in order produce an identical filter), or ``None`` on any violation. The
    grammars keep their ``^...$`` anchors so they stay valid ECMA-262 patterns
    for the advertised input schema.
    """
    if not isinstance(raw, list) or not (1 <= len(raw) <= _MAX_SCOPE_VALUES):
        return None
    grammar = _RE_SERVICE_NAME if kind == "SERVICE" else _RE_REGION_CODE
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            return None
        # Enforce the advertised maxLength for both kinds. The region grammar is
        # otherwise unbounded, so without this a very long grammar-valid region
        # would pass validation and be forwarded despite the schema's maxLength.
        if len(value) > _MAX_STR_INPUT:
            return None
        if grammar.fullmatch(value) is None:
            return None
        # Shared sensitive-shape rejection reuses the projection grammar family.
        # SERVICE names legitimately contain spaces/punctuation, so we validate
        # the sensitive-shape family via the TOKEN-free check below rather than a
        # single grammar: reject account-id runs, ARNs, URLs, and coordinates.
        if not _value_is_sensitive_safe(value):
            return None
        if value in seen:
            return None
        seen.add(value)
        out.append(value)
    # Sort for canonical stability: two inputs that differ only in order forward
    # an identical filter string and echo an identical scope.
    return sorted(out)


def _value_is_sensitive_safe(value: str) -> bool:
    """Reject a scope value carrying a sensitive coordinate shape.

    Applies explicit checks mirroring the projection module's sensitive-shape
    family: a 12-digit account-id run, an ``arn:`` prefix, any control
    character, any URL scheme, and a dotted network-coordinate run. SERVICE
    display names legitimately contain spaces and punctuation, so this is an
    explicit per-check function rather than a single grammar.
    """
    if _ACCOUNT_ID_RE.search(value):
        return False
    if value.startswith("arn:"):
        return False
    # Any control character is unsafe.
    if re.search(r"[\x00-\x1f\x7f-\x9f]", value):
        return False
    # Any URL scheme is unsafe.
    if re.search(r"[a-zA-Z][a-zA-Z0-9+.\-]*://", value):
        return False
    # A dotted network-coordinate run.
    if re.search(r"\d+(?:\.\d+){3}", value):
        return False
    return True


def _build_scope_filter(services: list[str] | None, regions: list[str] | None) -> tuple[str | None, Any]:
    """Build the canonical Cost Explorer Dimensions filter string and the scope
    echoed in the envelope. Returns (filter_json_or_None, scope)."""
    parts: list[dict[str, Any]] = []
    scope: dict[str, list[str]] = {}
    if services:
        parts.append({"Dimensions": {"Key": "SERVICE", "Values": list(services)}})
        scope["SERVICE"] = list(services)
    if regions:
        parts.append({"Dimensions": {"Key": "REGION", "Values": list(regions)}})
        scope["REGION"] = list(regions)
    if not parts:
        return None, "account"
    expression: dict[str, Any] = parts[0] if len(parts) == 1 else {"And": parts}
    filter_json = json.dumps(expression, sort_keys=True, separators=(",", ":"))
    return filter_json, scope


def _forecast_horizon_days(granularity: str) -> int:
    return _MAX_FORECAST_DAYS_DAILY if granularity == "DAILY" else _MAX_FORECAST_DAYS_MONTHLY


def _validate_forecast(inp: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str, int | None]:
    operation = inp.get("operation")
    metric = inp.get("metric")
    granularity = inp.get("granularity")
    start = inp.get("start_date")
    end = inp.get("end_date")
    if not isinstance(metric, str) or metric not in _CE_COST_METRICS:
        return None, _MSG_DENIED_INPUT, None
    if not isinstance(granularity, str) or granularity not in _CE_GRANULARITY:
        return None, _MSG_DENIED_INPUT, None
    if not isinstance(start, str) or not _DATE_RE.match(start):
        return None, _MSG_DENIED_INPUT, None
    if not isinstance(end, str) or not _DATE_RE.match(end):
        return None, _MSG_DENIED_INPUT, None
    try:
        # Standard library
        from datetime import date

        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError:
        return None, _MSG_DENIED_INPUT, None
    if end_d < start_d or (end_d - start_d).days > _forecast_horizon_days(granularity):
        return None, _MSG_DENIED_INPUT, None

    # Code-owned forecast scope: flat SERVICE / REGION lists -> canonical filter.
    services: list[str] | None = None
    regions: list[str] | None = None
    if "services" in inp:
        services = _valid_scope_values(inp.get("services"), "SERVICE")
        if services is None:
            return None, _MSG_DENIED_INPUT, None
    if "regions" in inp:
        regions = _valid_scope_values(inp.get("regions"), "REGION")
        if regions is None:
            return None, _MSG_DENIED_INPUT, None
    filter_json, scope = _build_scope_filter(services, regions)

    canonical: dict[str, Any] = {
        "operation": operation,
        "metric": metric,
        "granularity": granularity,
        "start_date": start,
        "end_date": end,
    }
    pil = inp.get("prediction_interval_level")
    if pil is not None:
        if isinstance(pil, bool) or not isinstance(pil, int) or not (70 <= pil <= 99):
            return None, _MSG_DENIED_INPUT, None
        canonical["prediction_interval_level"] = pil
    if filter_json is not None:
        canonical["filter"] = filter_json
    # The scope echoed in the envelope is carried out-of-band for the projector
    # under a private key the delegate never receives.
    canonical["__scope__"] = scope
    return canonical, "", None


def _validate_compute_optimizer(inp: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str, int | None]:
    operation = inp.get("operation")
    canonical: dict[str, Any] = {"operation": operation}
    # Compute Optimizer and its dependent EC2/Auto Scaling reads are granted only
    # in the deployment region. Bind the input region to that region: absent (the
    # delegate then defaults to the deployment region's AWS_REGION) or exactly
    # equal is allowed; anything else is denied. Forward the canonical region so
    # the delegate never queries another region.
    region = inp.get("region")
    if region is not None:
        if not isinstance(region, str) or region != settings.AWS_REGION:
            return None, _MSG_DENIED_INPUT, None
    canonical["region"] = settings.AWS_REGION
    ok, bounded = _bounded_max_results(inp)
    if not ok:
        return None, _MSG_DENIED_INPUT, None
    # Over-fetch by one so the projector can detect residual data even when the
    # upstream helper returns no continuation token.
    canonical["max_results"] = min(bounded + 1, MAX_RESULTS_CAP + 1)
    return canonical, "", bounded


def _validate_cost_optimization(inp: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str, int | None]:
    operation = inp.get("operation")
    canonical: dict[str, Any] = {"operation": operation}
    bound: int | None = None
    if operation == "list_recommendation_summaries":
        group_by = inp.get("group_by")
        if not isinstance(group_by, str) or group_by not in _COH_GROUP_BY:
            return None, _MSG_DENIED_INPUT, None
        canonical["group_by"] = group_by
    if operation == "get_recommendation":
        # resource_id carries the recommendation_id from list_recommendations.
        # The upstream MCP tool also requires a resource_type argument, but its
        # helper documents it as unused by the API call (only a presence check),
        # so the guard supplies a fixed code-owned placeholder rather than asking
        # the model for a meaningless value.
        resource_id = inp.get("resource_id")
        if not isinstance(resource_id, str) or valid_typed_string(resource_id, "identifier") is False:
            return None, _MSG_DENIED_INPUT, None
        canonical["resource_id"] = resource_id
        canonical["resource_type"] = _COH_RECOMMENDATION_RESOURCE_TYPE
    if operation in ("list_recommendations", "list_recommendation_summaries"):
        ok, bounded = _bounded_max_results(inp)
        if not ok:
            return None, _MSG_DENIED_INPUT, None
        bound = bounded
        canonical["max_results"] = min(bounded + 1, MAX_RESULTS_CAP + 1)
    return canonical, "", bound


# Per-tool: allowed operations, input validator, the projector keyed by op, a
# code-owned description, and a code-owned input schema advertised to the model.
def _forecast_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": sorted(_CE_FORECAST_OPS)},
            "metric": {"type": "string", "enum": sorted(_CE_COST_METRICS)},
            "granularity": {"type": "string", "enum": sorted(_CE_GRANULARITY)},
            "start_date": {
                "type": "string",
                "pattern": _DATE_RE.pattern,
                "description": (
                    "YYYY-MM-DD (inclusive). The forecast horizon is at most 3 months for DAILY "
                    "and 18 months for MONTHLY."
                ),
            },
            "end_date": {"type": "string", "pattern": _DATE_RE.pattern, "description": "YYYY-MM-DD (exclusive)."},
            "prediction_interval_level": {"type": "integer", "minimum": 70, "maximum": 99},
            "services": {
                "type": "array",
                "items": {"type": "string", "pattern": _RE_SERVICE_NAME.pattern, "maxLength": _MAX_STR_INPUT},
                "minItems": 1,
                "maxItems": _MAX_SCOPE_VALUES,
                "uniqueItems": True,
                "description": "1-10 AWS service display names to scope the forecast (e.g. 'Amazon GameLift').",
            },
            "regions": {
                "type": "array",
                "items": {"type": "string", "pattern": _RE_REGION_CODE.pattern, "maxLength": _MAX_STR_INPUT},
                "minItems": 1,
                "maxItems": _MAX_SCOPE_VALUES,
                "uniqueItems": True,
                "description": "1-10 region codes to scope the forecast (e.g. 'us-west-2').",
            },
        },
        "required": ["operation", "metric", "granularity", "start_date", "end_date"],
        "additionalProperties": False,
    }


def _compute_optimizer_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": sorted(_CO_OPS)},
            "region": {
                "type": "string",
                "enum": [settings.AWS_REGION],
                "description": "Must equal the deployment region, or be omitted.",
            },
            "max_results": {"type": "integer", "minimum": MAX_RESULTS_MIN, "maximum": MAX_RESULTS_CAP},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }


def _cost_optimization_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": sorted(_COH_OPS)},
            "group_by": {
                "type": "string",
                "enum": sorted(_COH_GROUP_BY),
                "description": "Required for list_recommendation_summaries (AccountId is not permitted).",
            },
            "max_results": {"type": "integer", "minimum": MAX_RESULTS_MIN, "maximum": MAX_RESULTS_CAP},
            "resource_id": {
                "type": "string",
                "description": "For get_recommendation: the recommendation_id returned by list_recommendations.",
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }


_TOOL_CONFIG: dict[str, dict[str, Any]] = {
    _COST_EXPLORER_TOOL: {
        "ops": _CE_FORECAST_OPS,
        "validate": _validate_forecast,
        "projector": lambda op, data, bound: project_forecast(data),
        "input_schema": _forecast_input_schema(),
        "description": (
            "Forecast future AWS cost via Cost Explorer. operation is getCostForecast. Scope the "
            "forecast with services (1-10 AWS service display names) and/or regions (1-10 region "
            "codes); the account-wide forecast is returned when neither is given, and the applied "
            "scope is echoed back. Historical cost/usage (getCostAndUsage*) is disabled; use "
            "get_cost_report for validated totals, rankings, and percentages. Results are bounded, "
            "estimated, and account identifiers are never returned."
        ),
    },
    _COMPUTE_OPTIMIZER_TOOL: {
        "ops": _CO_OPS,
        "validate": _validate_compute_optimizer,
        "projector": lambda op, data, bound: project_compute_optimizer(data, bound=bound),
        "input_schema": _compute_optimizer_input_schema(),
        "description": (
            "Performance rightsizing recommendations via AWS Compute Optimizer for EC2 instances "
            "or Auto Scaling groups. operation is one of get_ec2_instance_recommendations or "
            "get_auto_scaling_group_recommendations, scoped to the deployment region. Cross-account "
            "parameters are rejected; outputs are bounded and omit ARNs, account IDs, and tags."
        ),
    },
    _COST_OPTIMIZATION_TOOL: {
        "ops": _COH_OPS,
        "validate": _validate_cost_optimization,
        "projector": None,  # chosen per operation below
        "input_schema": _cost_optimization_input_schema(),
        "description": (
            "Cost savings recommendations via AWS Cost Optimization Hub: list_recommendation_summaries "
            "(requires group_by, excluding AccountId), list_recommendations, or get_recommendation "
            "(pass the recommendation_id from list_recommendations in resource_id). "
            "Outputs are bounded and omit ARNs, account IDs, and tags; savings values are estimated."
        ),
    },
}


def _coh_projector(operation: str, data: Mapping[str, Any], bound: int) -> dict[str, Any] | None:
    if operation == "list_recommendation_summaries":
        return project_cost_optimization_summaries(data, bound=bound)
    if operation == "get_recommendation":
        return project_cost_optimization_detail(data)
    return project_cost_optimization_recommendations(data, bound=bound)


# --------------------------------------------------------------------------- #
# Bounded delegate streaming                                                  #
# --------------------------------------------------------------------------- #
async def _drain_final_result(
    delegate: AgentTool, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
) -> Mapping[str, Any] | None:
    async def _run() -> Mapping[str, Any] | None:
        final: Mapping[str, Any] | None = None
        async for event in delegate.stream(tool_use, invocation_state, **kwargs):
            if isinstance(event, ToolResultEvent):
                final = cast(Mapping[str, Any], event.tool_result)
        return final

    try:
        return await asyncio.wait_for(_run(), timeout=DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Guarded tool                                                                #
# --------------------------------------------------------------------------- #
class _CostGuardedTool(AgentTool):
    """Guard a single allowed Billing MCP tool: constrain op, validate input,
    bound the delegate, and project output."""

    def __init__(self, delegate: AgentTool) -> None:
        super().__init__()
        self._delegate = delegate
        self._name = delegate.tool_name
        self._config = _TOOL_CONFIG[self._name]

    @property
    def tool_name(self) -> str:
        return str(self._delegate.tool_name)

    @property
    def tool_spec(self) -> ToolSpec:
        spec = cast(ToolSpec, dict(self._delegate.tool_spec))
        spec["description"] = self._config["description"]
        # Advertise a CODE-OWNED input schema: the operation enum, only the
        # canonical keys with their bounds, and the forecast scope parameters.
        # The upstream schema (filter/billing_view_arn/next_token/account_ids/…)
        # is never shown, so the model cannot be told about parameters the guard
        # rejects or silently drops.
        spec["inputSchema"] = {"json": dict(self._config["input_schema"])}
        return spec

    @property
    def tool_type(self) -> str:
        return str(self._delegate.tool_type)

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        name = self._name
        cid = uuid.uuid4().hex[:12]
        inp = tool_use.get("input")
        inp = inp if isinstance(inp, Mapping) else {}
        operation = inp.get("operation")

        # Specific redirect for the two blocked historical operations
        # (case-insensitive so a differently-cased name still redirects).
        if name == _COST_EXPLORER_TOOL and isinstance(operation, str) and operation.lower() in _CE_BLOCKED_OPS_LOWER:
            _log_outcome(name, _CODE_DENIED, cid)
            yield ToolResultEvent(
                {"toolUseId": tool_use_id, "status": "error", "content": [{"text": _MSG_BLOCKED_HISTORICAL}]}
            )
            return

        # Operation allowlist.
        if not isinstance(operation, str) or operation not in self._config["ops"]:
            _log_outcome(name, _CODE_DENIED, cid)
            yield ToolResultEvent(
                _sanitized_error(tool_use_id, str(operation), STATUS_DENIED, _CODE_DENIED, _MSG_DENIED_OP)
            )
            return

        # Reject any forbidden/cross-account input key before touching values.
        if any(key in inp for key in _FORBIDDEN_INPUT_KEYS):
            _log_outcome(name, _CODE_DENIED_INPUT, cid)
            yield ToolResultEvent(
                _sanitized_error(tool_use_id, operation, STATUS_DENIED, _CODE_DENIED_INPUT, _MSG_DENIED_INPUT)
            )
            return

        # Reject any key outside this operation's allowlist (never silently
        # dropped), so the model is never misled that an unapplied option took.
        if not _reject_unknown_keys(operation, inp):
            _log_outcome(name, _CODE_DENIED_INPUT, cid)
            yield ToolResultEvent(
                _sanitized_error(tool_use_id, operation, STATUS_DENIED, _CODE_DENIED_INPUT, _MSG_DENIED_INPUT)
            )
            return

        # Per-operation input validation + canonical delegate input (+ the model
        # row bound used both to over-fetch and to classify the projection).
        canonical, message, bound = self._config["validate"](inp)
        if canonical is None:
            _log_outcome(name, _CODE_DENIED_INPUT, cid)
            yield ToolResultEvent(_sanitized_error(tool_use_id, operation, STATUS_DENIED, _CODE_DENIED_INPUT, message))
            return

        # The scope is echoed to the model but must never be forwarded to the
        # delegate; strip the private key from the delegate input.
        scope = canonical.pop("__scope__", None)
        projector_bound = bound if isinstance(bound, int) else MAX_RESULTS_DEFAULT

        delegate_use: ToolUse = {**tool_use, "input": canonical}
        result = await _drain_final_result(self._delegate, delegate_use, invocation_state, **kwargs)
        if result is None:
            _log_outcome(name, _CODE_UNAVAILABLE, cid)
            yield ToolResultEvent(
                _sanitized_error(tool_use_id, operation, STATUS_INCOMPLETE, _CODE_UNAVAILABLE, _MSG_UNAVAILABLE)
            )
            return

        if _result_signals_error(result):
            status, code, msg = _classify_error(result)
            _log_outcome(name, code, cid)
            yield ToolResultEvent(_sanitized_error(tool_use_id, operation, status, code, msg))
            return

        payload = _extract_payload(result)
        if payload is None:
            _log_outcome(name, _CODE_MALFORMED, cid)
            yield ToolResultEvent(
                _sanitized_error(tool_use_id, operation, STATUS_INCOMPLETE, ERROR_MALFORMED_RESPONSE, _MSG_MALFORMED)
            )
            return

        try:
            if name == _COST_OPTIMIZATION_TOOL:
                projected = _coh_projector(operation, payload, projector_bound)
            else:
                projected = self._config["projector"](operation, payload, projector_bound)
        except Exception:
            projected = None
        if projected is None:
            _log_outcome(name, _CODE_MALFORMED, cid)
            yield ToolResultEvent(
                _sanitized_error(tool_use_id, operation, STATUS_INCOMPLETE, ERROR_MALFORMED_RESPONSE, _MSG_MALFORMED)
            )
            return

        _log_outcome(name, projected.get("status", "ok"), cid)
        yield ToolResultEvent(_ok(tool_use_id, operation, projected, scope=scope))


# --------------------------------------------------------------------------- #
# Provider                                                                    #
# --------------------------------------------------------------------------- #
class CostReportGuardedProvider(ToolProvider):
    """Expose only the three reviewed Billing MCP tools, each output-guarded.

    Every other current tool and any future-version tool is dropped so it is
    neither advertised nor invokable. Lifecycle methods are delegated.
    """

    def __init__(self, delegate: ToolProvider) -> None:
        self._delegate = delegate

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        tools = await self._delegate.load_tools(**kwargs)
        guarded: list[AgentTool] = []
        for tool in tools:
            if tool.tool_name in _ALLOWED_TOOLS:
                guarded.append(_CostGuardedTool(tool))
            else:
                logger.debug("cost_guard drop tool=other")
        return guarded

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self._delegate.add_consumer(consumer_id, **kwargs)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self._delegate.remove_consumer(consumer_id, **kwargs)


def guard_cost_mcp_client(server_name: str, client: ToolProvider) -> ToolProvider:
    """Guard the Billing MCP provider; return non-billing clients unchanged."""
    if server_name != _BILLING_MCP_SERVER:
        return client
    return CostReportGuardedProvider(client)
