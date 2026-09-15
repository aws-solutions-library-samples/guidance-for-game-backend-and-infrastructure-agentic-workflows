"""Serialization for shared cost report snapshots.

Converts an immutable :class:`~agents.cost_report.CostReportSnapshot` to and
from the minimum durable representation needed for deterministic reuse. Only the
validated report metadata and the unique canonical per-service aggregate decimal
strings are stored — never prompts, credentials, conversation content, or the
raw per-page source amounts (which are not needed to reuse a report).

The DynamoDB item stores the snapshot as a single JSON string attribute plus the
scope hash and TTL metadata. Aggregate amounts are decimal strings, so the JSON
round-trip preserves them exactly without float coercion.

Decode is strict and fails closed: a stored snapshot is only accepted if every
invariant still holds and the canonical report can be rebuilt, byte-for-byte,
from the persisted service aggregates through the same validated report-building
logic that produced it. Any deviation resolves to a generic not-found rather
than surfacing an unverified financial figure.
"""

from __future__ import annotations

# Standard library
import json
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Local modules
    from agents.cost_report import CostReportSnapshot
    from agents.cost_snapshot_store import SnapshotRecord

# Serialization format version for the stored snapshot payload.
#
# Schema-version policy (rolling reader window): a reader accepts any schema
# version it still understands. When the stored shape changes on a future
# revision, add the new reader/decoder alongside the existing one and keep
# decoding older-but-compatible versions for at least a full snapshot TTL, so a
# rolling deployment does not orphan snapshots written moments before by a
# not-yet-upgraded worker. Do NOT switch to rejecting every prior version at
# once — that would strand in-flight report IDs. A version this reader does not
# recognize still fails closed (returns not-found), which is safe.
SNAPSHOT_SCHEMA_VERSION = "1.0"

# DynamoDB attribute names. Kept here so the table, IAM, and serialization stay
# in agreement.
ATTR_REPORT_ID = "reportId"
ATTR_SCOPE_HASH = "scopeHash"
ATTR_EXPIRES_AT = "expiresAt"
ATTR_SCHEMA_VERSION = "schemaVersion"
ATTR_SNAPSHOT = "snapshot"

# Conservative encoded item-size ceiling, well below DynamoDB's hard 400 KB
# per-item limit, checked before PutItem so an oversized snapshot fails closed as
# a typed store error instead of being rejected by the service mid-write.
MAX_ITEM_BYTES = 380_000

# Defensive bounds on a single service aggregate decimal string. These reject a
# malformed or hostile payload before it is trusted: an absurd magnitude or an
# excessively long digit string cannot represent a real Cost Explorer aggregate
# and could otherwise drive pathological Decimal work.
_MAX_DECIMAL_STRING_LEN = 64
_MAX_ABS_AMOUNT = Decimal("1e15")


class SnapshotSerializationError(ValueError):
    """A snapshot payload is malformed, inconsistent, or failed re-validation.

    Raised by decode so a store can treat it as a fail-closed not-found, and by
    encode so a store can translate it into a typed store failure rather than an
    unhandled crash mid-write.
    """


def _parse_service_amount(value: Any) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotSerializationError("service aggregate amount must be a non-empty string")
    if len(value) > _MAX_DECIMAL_STRING_LEN:
        raise SnapshotSerializationError("service aggregate amount is too long")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotSerializationError("service aggregate amount is not a decimal") from exc
    if not amount.is_finite():
        raise SnapshotSerializationError("service aggregate amount is not finite")
    if abs(amount) > _MAX_ABS_AMOUNT:
        raise SnapshotSerializationError("service aggregate amount is out of bounds")
    return amount


def snapshot_to_json(snapshot: "CostReportSnapshot") -> str:
    """Serialize a snapshot to a compact, deterministic JSON string.

    Persists strict report metadata plus the unique canonical per-service
    aggregate decimal strings. Raises :class:`SnapshotSerializationError` if the
    snapshot contains a duplicate or empty service name, so a store never writes
    an item that would fail its own strict decode.
    """
    seen: set[str] = set()
    services: list[dict[str, str]] = []
    for raw in snapshot.raw_services:
        if not isinstance(raw.service, str) or not raw.service.strip():
            raise SnapshotSerializationError("service name must be a non-empty string")
        if raw.service in seen:
            raise SnapshotSerializationError("duplicate service name in snapshot")
        seen.add(raw.service)
        services.append({"service": raw.service, "amount": format(raw.amount, "f")})

    payload = {
        "report": snapshot.report.model_dump(by_alias=True, mode="json"),
        "services": services,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


def _validate_report_metadata(report: Any) -> None:
    """Recheck strict, deployment-independent metadata invariants on a report."""
    # Local modules
    from agents.cost_report import _SUPPORTED_METRICS  # imported lazily to avoid a cycle

    if report.schema_version != "1.0":
        raise SnapshotSerializationError("unexpected report schema version")
    if report.source != "AWS Cost Explorer":
        raise SnapshotSerializationError("unexpected report source")
    if report.metric not in _SUPPORTED_METRICS:
        raise SnapshotSerializationError("unsupported report metric")
    if not isinstance(report.currency, str) or not report.currency.strip():
        raise SnapshotSerializationError("missing report currency")
    validation = report.validation
    if not (validation.total_matches and validation.rankings_match and validation.percentages_match):
        raise SnapshotSerializationError("stored report validation flags are not all satisfied")


def _parse_period_dates(report: Any) -> tuple[Any, Any, Any]:
    # Standard library
    from datetime import date, timedelta

    try:
        start = date.fromisoformat(report.period.start)
        end_inclusive = date.fromisoformat(report.period.end_inclusive)
        end_exclusive = date.fromisoformat(report.period.end_exclusive)
        expected_end_exclusive = end_inclusive + timedelta(days=1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SnapshotSerializationError("report period dates are malformed") from exc
    if end_inclusive < start or end_exclusive != expected_end_exclusive:
        raise SnapshotSerializationError("report period dates are inconsistent")
    return start, end_inclusive, end_exclusive


def snapshot_from_json(text: str) -> "CostReportSnapshot":
    """Reconstruct a snapshot from its JSON string, revalidating every invariant.

    The stored report is only trusted if the canonical report rebuilt from the
    persisted service aggregates — through the same validated report-building
    logic used at creation — is byte-for-byte identical to it. This rechecks the
    ranking, rounding, totals, and percentage reconciliation deterministically.

    Raises:
        SnapshotSerializationError: if the payload is malformed, inconsistent,
            or fails re-validation.
    """
    # Local modules
    from agents.cost_report import (
        CostReport,
        CostReportError,
        CostReportSnapshot,
        RawServiceCost,
        _build_report,
    )

    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise SnapshotSerializationError("snapshot payload is not valid JSON") from exc

    if not isinstance(payload, dict) or "report" not in payload or "services" not in payload:
        raise SnapshotSerializationError("snapshot payload is missing required fields")

    try:
        report = CostReport.model_validate(payload["report"])
    except Exception as exc:  # pydantic ValidationError and friends
        raise SnapshotSerializationError("stored report is not a valid cost report") from exc

    _validate_report_metadata(report)
    start, end_inclusive, end_exclusive = _parse_period_dates(report)

    service_entries = payload["services"]
    if not isinstance(service_entries, list) or not service_entries:
        raise SnapshotSerializationError("snapshot services must be a non-empty list")

    seen: set[str] = set()
    raw_services: list[RawServiceCost] = []
    for entry in service_entries:
        if not isinstance(entry, dict):
            raise SnapshotSerializationError("snapshot service entry must be an object")
        service = entry.get("service")
        if not isinstance(service, str) or not service.strip():
            raise SnapshotSerializationError("snapshot service name must be a non-empty string")
        if service in seen:
            raise SnapshotSerializationError("duplicate service name in snapshot")
        seen.add(service)
        amount = _parse_service_amount(entry.get("amount"))
        # source_amounts is intentionally not persisted; reuse only needs the
        # canonical aggregate. Restore with an empty tuple.
        raw_services.append(RawServiceCost(service=service, source_amounts=(), amount=amount))

    # Rebuild the canonical report from the persisted aggregates and require an
    # exact match. _build_report re-runs every deterministic invariant and raises
    # if validation fails; a mismatch means the stored report was tampered with
    # or is inconsistent, so we fail closed.
    #
    # An adversarial-but-finite payload can also drive the canonical rebuild into
    # a decimal arithmetic failure (e.g. near-canceling aggregates that leave a
    # tiny non-zero total and blow up the percentage computation past the decimal
    # context precision, raising InvalidOperation). Those are expected, hostile
    # inputs — translate the decimal/arithmetic family into a typed serialization
    # error so record_from_item resolves to a generic not-found rather than
    # surfacing an unhandled crash. Programming errors (TypeError, AttributeError,
    # ...) are deliberately NOT caught here.
    try:
        rebuilt = _build_report(
            tuple(raw_services),
            report_id=report.report_id,
            metric=report.metric,
            currency=report.currency,
            start=start,
            end_inclusive=end_inclusive,
            end_exclusive=end_exclusive,
            queried_at=report.queried_at,
            estimated=report.estimated,
        )
    except CostReportError as exc:
        raise SnapshotSerializationError("stored report failed deterministic re-validation") from exc
    except ArithmeticError as exc:
        # InvalidOperation, DivisionByZero, Overflow, and other decimal/arithmetic
        # failures raised while reconstructing the canonical report.
        raise SnapshotSerializationError("stored report failed canonical arithmetic reconstruction") from exc

    if rebuilt != report:
        raise SnapshotSerializationError("stored report does not match its canonical reconstruction")

    return CostReportSnapshot(report=report, raw_services=tuple(raw_services))


def estimate_item_size(item: dict[str, Any]) -> int:
    """Estimate the encoded byte size of a DynamoDB item, conservatively.

    Sums UTF-8 attribute-name lengths plus attribute-value lengths (string and
    number values are the only types used here). This over- rather than
    under-counts relative to DynamoDB's own accounting, which is the safe
    direction for a pre-write budget check.
    """
    total = 0
    for name, value in item.items():
        total += len(name.encode("utf-8"))
        for _type, raw in value.items():
            total += len(str(raw).encode("utf-8"))
    return total


def record_to_item(record: "SnapshotRecord") -> dict[str, Any]:
    """Render a snapshot record as a low-level DynamoDB item.

    Raises:
        SnapshotSerializationError: if the snapshot cannot be serialized.
    """
    return {
        ATTR_REPORT_ID: {"S": record.report_id},
        ATTR_SCOPE_HASH: {"S": record.scope_hash},
        ATTR_EXPIRES_AT: {"N": str(int(record.expires_at))},
        ATTR_SCHEMA_VERSION: {"S": SNAPSHOT_SCHEMA_VERSION},
        ATTR_SNAPSHOT: {"S": snapshot_to_json(record.snapshot)},
    }


def record_from_item(item: dict[str, Any]) -> "SnapshotRecord | None":
    """Parse a DynamoDB item into a snapshot record, or ``None`` if malformed.

    Malformed, version-mismatched, inconsistent, or report-ID-mismatched items
    resolve to ``None`` so a reuse fails closed with a generic not-found rather
    than surfacing a partial or tampered snapshot.
    """
    # Local modules
    from agents.cost_snapshot_store import SnapshotRecord

    try:
        report_id = item[ATTR_REPORT_ID]["S"]
        scope_hash = item[ATTR_SCOPE_HASH]["S"]
        expires_at = int(item[ATTR_EXPIRES_AT]["N"])
        schema_version = item[ATTR_SCHEMA_VERSION]["S"]
        snapshot_text = item[ATTR_SNAPSHOT]["S"]
    except (KeyError, TypeError, ValueError):
        return None

    if schema_version != SNAPSHOT_SCHEMA_VERSION:
        return None

    try:
        snapshot = snapshot_from_json(snapshot_text)
    except SnapshotSerializationError:
        return None

    # The item key (outer) must agree with the report ID embedded in the
    # validated snapshot (inner); a mismatch means a corrupted or swapped item.
    if snapshot.report.report_id != report_id:
        return None

    return SnapshotRecord(
        report_id=report_id,
        scope_hash=scope_hash,
        snapshot=snapshot,
        expires_at=expires_at,
    )
