"""Regression coverage for performance benchmark result persistence."""

# Standard library
import importlib.util
import json
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_BENCHMARK_PATH = Path(__file__).parents[1] / "performance" / "timing_benchmark.py"


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location("timing_benchmark_for_test", _BENCHMARK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_save_results_writes_valid_json_to_requested_path(tmp_path):
    benchmark = _load_benchmark_module()
    expected = {"timestamp": "2026-01-01T00:00:00Z", "summary": {"overall": {"avg_seconds": 1.25}}}
    target = tmp_path / "benchmark.json"

    saved = benchmark.save_results(expected, str(target))

    assert Path(saved) == target
    assert json.loads(target.read_text(encoding="utf-8")) == expected
