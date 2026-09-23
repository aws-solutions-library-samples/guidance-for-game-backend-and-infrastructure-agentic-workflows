"""Naive-UTC parsing regression for the E5 runtime service (#439).

``_parse_iso`` previously called ``.astimezone(timezone.utc)`` on the result of
``datetime.fromisoformat`` without asserting the parsed value carried an offset.
A timestamp with no offset (e.g. ``2026-09-21T19:12:52`` without a trailing
``Z``) parses to a *naive* datetime, and ``.astimezone(utc)`` on a naive value
silently reinterprets it in the host's LOCAL timezone — a wall-clock/UTC skew of
up to a day depending on where the executor runs. The runtime must never guess a
timezone: a timestamp without an explicit UTC marker fails closed.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime.service import AutonomyRuntimeError, _parse_iso


@pytest.mark.unit
def test_parse_iso_accepts_explicit_utc_z_suffix() -> None:
    parsed = _parse_iso("2026-09-21T19:12:52Z")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None
    assert parsed.isoformat() == "2026-09-21T19:12:52+00:00"


@pytest.mark.unit
def test_parse_iso_accepts_explicit_offset() -> None:
    parsed = _parse_iso("2026-09-21T21:12:52+02:00")
    # Normalized to UTC regardless of host timezone.
    assert parsed.isoformat() == "2026-09-21T19:12:52+00:00"


@pytest.mark.unit
def test_parse_iso_rejects_naive_timestamp_rather_than_assuming_local() -> None:
    with pytest.raises(AutonomyRuntimeError):
        _parse_iso("2026-09-21T19:12:52")
