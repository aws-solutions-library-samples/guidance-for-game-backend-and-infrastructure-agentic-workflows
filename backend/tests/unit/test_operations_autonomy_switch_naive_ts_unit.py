"""The autonomy switch must never coerce a naive timestamp (issue #439).

``datetime.astimezone`` on a *naive* value silently reinterprets it in the
host's local timezone, so an ``issued_at``/``not_after`` without an explicit UTC
marker would skew freshness by the host's UTC offset and could keep a stale
switch "fresh". The switch must require an explicit timezone / normalized ``Z``
and reject an offset-less timestamp rather than assuming it is UTC.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone

# Third-party packages
import pytest

# Local modules
from operations.autonomy_switch import AUTONOMY_SWITCH_DOCUMENT_VERSION, validate_autonomy_switch_document

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _document(*, issued_at: str, not_after: str) -> dict[str, object]:
    return {
        "autonomy_switch_version": AUTONOMY_SWITCH_DOCUMENT_VERSION,
        "config_version": 1,
        "issued_at": issued_at,
        "not_after": not_after,
        "autonomy_enabled": True,
        "capabilities": {"gamelift.capacity-adjustment": {"autonomous_write": True}},
    }


@pytest.mark.unit
def test_naive_issued_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(_document(issued_at="2026-01-01T11:00:00", not_after="2026-01-01T13:00:00Z"))


@pytest.mark.unit
def test_naive_not_after_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_autonomy_switch_document(_document(issued_at="2026-01-01T11:00:00Z", not_after="2026-01-01T13:00:00"))


@pytest.mark.unit
def test_explicit_z_timestamps_are_accepted() -> None:
    # A normalized Z instant is accepted (no raise).
    validate_autonomy_switch_document(_document(issued_at="2026-01-01T11:00:00Z", not_after="2026-01-01T13:00:00Z"))


@pytest.mark.unit
def test_explicit_offset_timestamps_are_accepted() -> None:
    # An explicit non-Z UTC offset is also acceptable.
    validate_autonomy_switch_document(
        _document(issued_at="2026-01-01T06:00:00-05:00", not_after="2026-01-01T13:00:00Z")
    )
