"""
Security utilities.

Implements security controls for:
- Input validation and sanitization (BSC33, GenAI prompt validation)
- Encryption context for KMS operations (BSC21)
- Data leakage protection
- Request authorization helpers
"""

from __future__ import annotations

# Standard library
import hashlib
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Third-party packages
    from loguru import Logger

# Import logger lazily to avoid circular imports
_logger: Logger | None = None


def _get_logger() -> Logger:
    """Get logger instance, importing lazily."""
    global _logger
    if _logger is None:
        # Local modules
        from utils.logger import logger as app_logger

        _logger = app_logger
    return _logger


# Maximum prompt length (characters)
MAX_PROMPT_LENGTH = 32000

# Maximum conversation history messages
MAX_HISTORY_MESSAGES = 100

# Patterns that may indicate prompt injection attempts
INJECTION_PATTERNS = [
    r"ignore\s+(previous|all|above)\s+(instructions?|prompts?)",
    r"disregard\s+(previous|all|above)",
    r"forget\s+(everything|all|previous)",
    r"you\s+are\s+now\s+(?:a|an)\s+",
    r"new\s+instructions?:",
    r"system\s*:\s*",
    r"<\s*system\s*>",
    r"\[\s*system\s*\]",
]

# Sensitive data patterns to detect and warn about
SENSITIVE_PATTERNS = {
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "aws_secret_key": r"[A-Za-z0-9/+=]{40}",
    "credit_card": r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    "ip_address": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
}


class InputValidationError(Exception):
    """Raised when input validation fails."""

    pass


class SecurityViolationError(Exception):
    """Raised when a security violation is detected."""

    pass


def validate_prompt(prompt: str, strict_mode: bool = False) -> str:
    """
    Validate and sanitize user prompt input.

    Args:
        prompt: Raw user input prompt
        strict_mode: If True, raises exceptions. If False, sanitizes and warns.

    Returns:
        Sanitized prompt string

    Raises:
        InputValidationError: If validation fails in strict mode
    """
    if not prompt:
        raise InputValidationError("Prompt cannot be empty")

    if not isinstance(prompt, str):
        raise InputValidationError("Prompt must be a string")

    logger = _get_logger()

    # Length validation
    if len(prompt) > MAX_PROMPT_LENGTH:
        if strict_mode:
            raise InputValidationError(f"Prompt exceeds maximum length of {MAX_PROMPT_LENGTH} characters")
        logger.warning(f"Prompt truncated from {len(prompt)} to {MAX_PROMPT_LENGTH} characters")
        prompt = prompt[:MAX_PROMPT_LENGTH]

    # Check for potential injection patterns
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            logger.warning(f"Potential prompt injection detected: pattern={pattern[:30]}...")
            if strict_mode:
                raise SecurityViolationError("Potential prompt injection detected")
            # In non-strict mode, we log but allow the request to proceed
            # The guardrails will provide additional protection
            break  # Only log once per prompt

    # Check for sensitive data (warn only, don't block)
    for data_type, pattern in SENSITIVE_PATTERNS.items():
        if re.search(pattern, prompt):
            logger.warning(f"Potentially sensitive data detected in prompt: type={data_type}")

    # Basic sanitization - remove null bytes and control characters
    prompt = prompt.replace("\x00", "")
    prompt = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", prompt)

    return prompt.strip()


def validate_user_context(context: dict | None) -> dict:
    """
    Validate user context dictionary.

    Args:
        context: User context dictionary

    Returns:
        Validated context dictionary
    """
    if context is None:
        return {}

    if not isinstance(context, dict):
        _get_logger().warning("Invalid user context type, using empty context")
        return {}

    validated = {}

    # Whitelist of allowed context keys
    allowed_keys = {
        "user_id",
        "session_id",
        "thread_id",
        "username",
        "display_name",
        "email",
        "auth_type",
        "actor_id",
        "groups",
        "is_admin",
    }

    for key in allowed_keys:
        if key in context:
            value = context[key]
            # Validate string values
            if isinstance(value, str):
                # Limit string length
                validated[key] = value[:500] if len(value) > 500 else value
            elif isinstance(value, (bool, int)):
                validated[key] = value
            elif isinstance(value, list):
                # For lists (like groups), validate each item
                validated[key] = [str(item)[:100] for item in value[:20]]

    return validated


def validate_conversation_history(history: list | None) -> list:
    """
    Validate conversation history.

    Args:
        history: List of conversation messages

    Returns:
        Validated history list
    """
    if history is None:
        return []

    if not isinstance(history, list):
        _get_logger().warning("Invalid conversation history type")
        return []

    validated = []
    for msg in history[:MAX_HISTORY_MESSAGES]:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role in ("user", "assistant", "system") and isinstance(content, str):
                validated.append({"role": role, "content": content[:MAX_PROMPT_LENGTH]})

    return validated


def create_encryption_context(
    resource_type: str,
    resource_id: str,
    user_id: str | None = None,
    additional_context: dict | None = None,
) -> dict[str, str]:
    """
    Create encryption context for KMS operations.

    Encryption context provides additional authenticated data (AAD) that is
    logged with CloudTrail and must match on decryption.

    Args:
        resource_type: Type of resource being encrypted (e.g., "conversation", "memory")
        resource_id: Unique identifier for the resource
        user_id: Optional user ID for user-scoped encryption
        additional_context: Optional additional context key-value pairs

    Returns:
        Dictionary suitable for KMS encryption context
    """
    context = {
        "service": "game-agent",
        "resource_type": resource_type,
        "resource_id": str(resource_id),
    }

    if user_id:
        context["user_id"] = str(user_id)

    if additional_context:
        for key, value in additional_context.items():
            # Encryption context values must be strings
            context[str(key)] = str(value)

    return context


def hash_sensitive_data(data: str, salt: str = "") -> str:
    """
    Create a one-way hash of sensitive data for logging/comparison.

    Args:
        data: Sensitive data to hash
        salt: Optional salt for the hash

    Returns:
        SHA-256 hash of the data
    """
    return hashlib.sha256((salt + data).encode()).hexdigest()


def sanitize_log_data(data: Any, max_length: int = 200) -> str:
    """
    Sanitize data for safe logging, redacting sensitive information.

    Args:
        data: Data to sanitize
        max_length: Maximum length of output string

    Returns:
        Sanitized string safe for logging
    """
    if data is None:
        return "None"

    text = str(data)

    # Redact sensitive patterns
    for data_type, pattern in SENSITIVE_PATTERNS.items():
        text = re.sub(pattern, f"[REDACTED_{data_type.upper()}]", text)

    # Truncate if too long
    if len(text) > max_length:
        text = text[:max_length] + "..."

    return text


def verify_request_authorization(
    user_id: str | None,
    required_groups: list[str] | None = None,
    user_groups: list[str] | None = None,
    require_authentication: bool = True,
) -> bool:
    """
    Verify request authorization based on user identity and groups.

    Args:
        user_id: User identifier
        required_groups: Groups required for this operation (any match)
        user_groups: Groups the user belongs to
        require_authentication: Whether authentication is required

    Returns:
        True if authorized, False otherwise
    """
    logger = _get_logger()

    # Check authentication requirement
    if require_authentication and not user_id:
        logger.warning("Authorization failed: No user ID provided")
        return False

    # If no group requirement, authentication alone is sufficient
    if not required_groups:
        return True

    # Check group membership
    if not user_groups:
        logger.warning("Authorization failed: No user groups provided")
        return False

    # User must be in at least one required group
    if not any(group in user_groups for group in required_groups):
        logger.warning(
            f"Authorization failed: User not in required groups. " f"Required: {required_groups}, Has: {user_groups}"
        )
        return False

    return True


def get_rate_limit_key(user_id: str | None, endpoint: str) -> str:
    """
    Generate a rate limiting key for a user/endpoint combination.

    Args:
        user_id: User identifier (or IP for unauthenticated requests)
        endpoint: API endpoint being accessed

    Returns:
        Rate limit key string
    """
    identifier = user_id or "anonymous"
    return f"ratelimit:{identifier}:{endpoint}"


# Standard library
# ---------------------------------------------------------------------------
# In-memory sliding-window rate limiter
# Addresses Well-Architected GenAI Lens: Operational Excellence 2.2
# ---------------------------------------------------------------------------
import collections
import os
import threading
import time as _time

# Third-party packages
from cachetools import TTLCache

_rate_limit_lock = threading.Lock()
# Bounded so a flood of distinct keys (per-user / per-IP) can't grow the dict
# without limit. Idle keys expire after the longest plausible window; the cap is
# a hard backstop. An evicted key simply starts a fresh window (correct, fail-open
# only after inactivity). Sized generously for concurrent active callers.
_RATE_LIMIT_MAX_KEYS = int(os.getenv("GBAW_RATE_LIMIT_MAX_KEYS", "10000"))
_RATE_LIMIT_KEY_TTL_SECONDS = int(os.getenv("GBAW_RATE_LIMIT_KEY_TTL_SECONDS", "3600"))
_rate_limit_windows: "TTLCache[str, collections.deque]" = TTLCache(
    maxsize=_RATE_LIMIT_MAX_KEYS, ttl=_RATE_LIMIT_KEY_TTL_SECONDS
)


class RateLimitExceeded(Exception):
    """Raised when a caller exceeds the configured request rate."""


def check_rate_limit(
    key: str,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Enforce a per-key sliding-window rate limit.

    Args:
        key: Rate-limit key (from get_rate_limit_key).
        max_requests: Maximum allowed requests in the window.
        window_seconds: Window duration in seconds.

    Raises:
        RateLimitExceeded: If the caller has exceeded the limit.
    """
    now = _time.monotonic()
    with _rate_limit_lock:
        window = _rate_limit_windows.setdefault(key, collections.deque())
        # Evict expired timestamps
        while window and window[0] <= now - window_seconds:
            window.popleft()
        if len(window) >= max_requests:
            raise RateLimitExceeded(
                f"Rate limit exceeded ({max_requests} requests per {window_seconds}s). Please try again shortly."
            )
        window.append(now)
