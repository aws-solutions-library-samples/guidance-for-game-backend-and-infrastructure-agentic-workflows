"""
Resilience utilities.

Provides retry-with-backoff decorator for transient AWS failures.
Addresses Well-Architected GenAI Lens: Reliability 2
(Redundant connections between model endpoints and infrastructure)
"""

# Standard library
import functools
import random
import time

# Local modules
from utils.logger import logger

# Retryable boto3/Bedrock exception names
_RETRYABLE_ERRORS = (
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
)


def retry_with_backoff(max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    """Decorator: retry on transient errors with exponential backoff + jitter.

    Args:
        max_attempts: Total attempts (including the first call).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Cap on delay between retries.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    exc_name = type(exc).__name__
                    # Also check nested cause for boto ClientError
                    error_code = ""
                    if hasattr(exc, "response"):
                        error_code = exc.response.get("Error", {}).get("Code", "")

                    retryable = exc_name in _RETRYABLE_ERRORS or error_code in _RETRYABLE_ERRORS
                    if not retryable or attempt == max_attempts:
                        raise

                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    jitter = random.uniform(0, delay * 0.25)
                    sleep_time = delay + jitter
                    logger.warning(
                        f"⚠️ {fn.__name__} attempt {attempt}/{max_attempts} failed "
                        f"({exc_name}: {exc}), retrying in {sleep_time:.1f}s"
                    )
                    last_exc = exc
                    time.sleep(sleep_time)

            raise last_exc  # pragma: no cover

        return wrapper

    return decorator
