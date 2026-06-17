"""
Timing utilities for performance monitoring and bottleneck identification.
"""

# Standard library
import functools
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

# Local modules
from utils.logger import logger


class TimingMetrics:
    """Collect and track timing metrics."""

    def __init__(self):
        self.metrics: Dict[str, list] = {}

    def record(self, operation: str, duration: float, details: Optional[Dict] = None):
        """Record a timing metric."""
        if operation not in self.metrics:
            self.metrics[operation] = []

        entry = {"duration": duration, "timestamp": time.time(), "details": details or {}}
        self.metrics[operation].append(entry)

        # Log the timing
        details_str = f" | {details}" if details else ""
        logger.info(f"⏱️  {operation}: {duration:.3f}s{details_str}")

    def get_stats(self, operation: str) -> Dict:
        """Get statistics for an operation."""
        if operation not in self.metrics:
            return {}

        durations = [m["duration"] for m in self.metrics[operation]]
        return {
            "count": len(durations),
            "total": sum(durations),
            "avg": sum(durations) / len(durations),
            "min": min(durations),
            "max": max(durations),
            "recent": durations[-5:] if len(durations) >= 5 else durations,
        }


# Global metrics collector
timing_metrics = TimingMetrics()


@contextmanager
def time_operation(operation_name: str, details: Optional[Dict] = None):
    """Context manager to time an operation."""
    start_time = time.time()
    logger.debug(f"🚀 Starting: {operation_name}")

    try:
        yield
    finally:
        duration = time.time() - start_time
        timing_metrics.record(operation_name, duration, details)


def time_function(operation_name: Optional[str] = None):
    """Decorator to time function execution."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            name = operation_name or f"{func.__module__}.{func.__name__}"

            with time_operation(name):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def log_performance_summary():
    """Log a summary of all timing metrics."""
    logger.info("📊 PERFORMANCE SUMMARY")
    logger.info("=" * 50)

    for operation, stats in timing_metrics.metrics.items():
        stats_summary = timing_metrics.get_stats(operation)
        if stats_summary:
            logger.info(f"{operation}:")
            logger.info(f"  Count: {stats_summary['count']}")
            logger.info(f"  Total: {stats_summary['total']:.3f}s")
            logger.info(f"  Average: {stats_summary['avg']:.3f}s")
            logger.info(f"  Min/Max: {stats_summary['min']:.3f}s / {stats_summary['max']:.3f}s")
            logger.info(f"  Recent: {[f'{d:.3f}s' for d in stats_summary['recent']]}")
            logger.info("")
