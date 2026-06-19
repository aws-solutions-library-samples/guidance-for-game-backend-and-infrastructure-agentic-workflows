"""Unit tests for user-name extraction regex (#139).

The optional second-word capture (for real surnames like "John Smith") must not
greedily swallow a sentence connector: "my name is Zephyr and I run X" must
yield "Zephyr", not "Zephyr And".
"""

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


def _extract(text):
    # Local modules
    from utils.semantic_memory import _NAME_PATTERNS

    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


@pytest.mark.parametrize(
    "text,expected",
    [
        # The original bug: connector after a single-word name must be dropped.
        ("My name is Zephyr and I run the Nebula fleet.", "Zephyr"),
        ("I'm Pico but you can call me admin", "Pico"),
        ("I am Bob who manages the cluster", "Bob"),
        ("My name is Sarah, the operator", "Sarah"),
        # Genuine two-word names must still be captured in full.
        ("My name is John Smith.", "John Smith"),
        ("I am Sarah Connor", "Sarah Connor"),
        # Single-word names unchanged.
        ("My name is Pico", "Pico"),
        # A surname that merely starts with a connector's letters is NOT blocked.
        ("I'm Anderson from ops", "Anderson"),
    ],
)
def test_name_extraction_stops_at_connectors(text, expected):
    assert _extract(text) is not None, f"no name extracted from {text!r}"
    assert _extract(text).lower() == expected.lower()
