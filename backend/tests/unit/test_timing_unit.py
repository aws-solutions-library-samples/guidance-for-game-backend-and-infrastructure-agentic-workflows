#!/usr/bin/env python3
"""Unit tests for timing utilities - behavioral testing."""

# Standard library
import os
import sys
import time
from unittest.mock import Mock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from utils.timing import TimingMetrics, time_function, time_operation, timing_metrics


class TestTimingMetrics:
    """Test TimingMetrics class behavior."""

    def test_metrics_initialization(self):
        """Test metrics object initializes correctly."""
        metrics = TimingMetrics()
        assert metrics.metrics == {}

    def test_record_new_operation(self):
        """Test recording a new operation creates entry."""
        metrics = TimingMetrics()
        metrics.record("test_op", 1.5, {"key": "value"})

        assert "test_op" in metrics.metrics
        assert len(metrics.metrics["test_op"]) == 1
        assert metrics.metrics["test_op"][0]["duration"] == 1.5
        assert metrics.metrics["test_op"][0]["details"]["key"] == "value"

    def test_record_multiple_operations(self):
        """Test recording multiple operations accumulates correctly."""
        metrics = TimingMetrics()
        metrics.record("test_op", 1.0)
        metrics.record("test_op", 2.0)
        metrics.record("other_op", 3.0)

        assert len(metrics.metrics["test_op"]) == 2
        assert len(metrics.metrics["other_op"]) == 1

    def test_get_stats_empty_operation(self):
        """Test get_stats returns empty dict for non-existent operation."""
        metrics = TimingMetrics()
        stats = metrics.get_stats("nonexistent")
        assert stats == {}

    def test_get_stats_calculates_correctly(self):
        """Test get_stats calculates statistics correctly."""
        metrics = TimingMetrics()
        metrics.record("test_op", 1.0)
        metrics.record("test_op", 2.0)
        metrics.record("test_op", 3.0)

        stats = metrics.get_stats("test_op")
        assert stats["count"] == 3
        assert stats["total"] == 6.0
        assert stats["avg"] == 2.0
        assert stats["min"] == 1.0
        assert stats["max"] == 3.0
        assert stats["recent"] == [1.0, 2.0, 3.0]


class TestTimeOperation:
    """Test time_operation context manager behavior."""

    @patch("utils.timing.timing_metrics")
    def test_time_operation_records_duration(self, mock_metrics):
        """Test time_operation records timing correctly."""
        with time_operation("test_operation"):
            time.sleep(0.01)  # Small delay

        # Verify record was called
        mock_metrics.record.assert_called_once()
        call_args = mock_metrics.record.call_args
        assert call_args[0][0] == "test_operation"  # operation name
        assert call_args[0][1] > 0  # duration should be positive

    @patch("utils.timing.timing_metrics")
    def test_time_operation_with_details(self, mock_metrics):
        """Test time_operation passes details correctly."""
        details = {"param": "value"}
        with time_operation("test_op", details):
            pass

        call_args = mock_metrics.record.call_args
        assert call_args[0][2] == details  # details parameter

    @patch("utils.timing.timing_metrics")
    def test_time_operation_handles_exceptions(self, mock_metrics):
        """Test time_operation still records timing even if exception occurs."""
        with pytest.raises(ValueError):
            with time_operation("test_op"):
                raise ValueError("Test exception")

        # Should still record the timing
        mock_metrics.record.assert_called_once()


class TestTimeFunction:
    """Test time_function decorator behavior."""

    @patch("utils.timing.timing_metrics")
    def test_time_function_decorator_basic(self, mock_metrics):
        """Test time_function decorator works correctly."""

        @time_function("test_func")
        def sample_function(x, y):
            return x + y

        result = sample_function(2, 3)
        assert result == 5

        # Verify timing was recorded
        mock_metrics.record.assert_called_once()
        call_args = mock_metrics.record.call_args
        assert call_args[0][0] == "test_func"

    @patch("utils.timing.timing_metrics")
    def test_time_function_auto_naming(self, mock_metrics):
        """Test time_function auto-generates names when not provided."""

        @time_function()
        def sample_function():
            return "test"

        result = sample_function()
        assert result == "test"

        # Verify timing was recorded with auto-generated name
        mock_metrics.record.assert_called_once()
        call_args = mock_metrics.record.call_args
        assert "sample_function" in call_args[0][0]

    @patch("utils.timing.timing_metrics")
    def test_time_function_preserves_function_metadata(self, mock_metrics):
        """Test time_function preserves original function metadata."""

        @time_function("test")
        def documented_function():
            """This is a test function."""
            return "result"

        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ == "This is a test function."


class TestGlobalTimingMetrics:
    """Test global timing_metrics behavior."""

    def test_global_metrics_exists(self):
        """Test global timing_metrics object exists."""
        assert timing_metrics is not None
        assert isinstance(timing_metrics, TimingMetrics)

    def test_global_metrics_shared_state(self):
        """Test global metrics maintains state across calls."""
        # Clear any existing metrics
        timing_metrics.metrics.clear()

        with time_operation("global_test"):
            pass

        assert "global_test" in timing_metrics.metrics
        assert len(timing_metrics.metrics["global_test"]) == 1
