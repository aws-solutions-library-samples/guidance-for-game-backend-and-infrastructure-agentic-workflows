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
import hmac
import os
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

    validated: dict[str, Any] = {}

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
        "client_id",
        "audience",
        "groups",
        "scopes",
        "tenant",
        "workspace",
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


# Per-process redaction key. Correlation tokens must let an operator tie several
# log lines back to the same principal or session within one running process,
# while never being reversible to the underlying identifier. A random key,
# generated once per process and never logged or configured, keeps a token
# stable within that process and makes it a non-dictionary-attackable value:
# without the key, a bare SHA-256 of a Cognito subject or thread id could be
# precomputed from guessable inputs. The key is intentionally not read from the
# environment — an environment-pinned value would sit in plaintext in the
# runtime and would be inherited by every MCP subprocess through the process
# environment. Because the key is random per process, tokens correlate only
# within a single process and never across processes, restarts, or tiers.
_LOG_REDACTION_KEY = os.urandom(32)

# Bounds for the correlation token length (hex characters). A caller cannot
# request the full digest (which would widen the surface for offline matching)
# or a token too short to be distinguishing.
_REDACT_MIN_LENGTH = 8
_REDACT_MAX_LENGTH = 32

# Control characters that must never reach a log sink verbatim. A CR or LF lets
# an externally influenced value forge an additional, attacker-controlled log
# line; the Unicode line/paragraph separators (U+2028, U+2029) and NEL (U+0085,
# inside the C1 range) are treated as line breaks by Unicode-aware viewers and
# by ``str.splitlines()``; the bidirectional-formatting controls can reorder
# how a line renders to hide injected content. All C0 controls, DEL, the full
# C1 range, the Unicode line separators, and the bidi controls are collapsed to
# a single space.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def normalize_log_value(value: Any) -> str:
    """
    Normalize a value for single-line, injection-safe logging.

    Replaces carriage returns, line feeds, tabs, other C0/C1/DEL control
    characters, the Unicode line and paragraph separators, and the
    bidirectional-formatting controls with a single space so an externally
    influenced field cannot create additional log records, be split across lines
    by a Unicode-aware consumer, or reorder a rendered line to hide content.

    Args:
        value: Any value to be rendered into a log message.

    Returns:
        A single-line string with control characters removed.
    """
    if value is None:
        return "None"
    return _CONTROL_CHARS_RE.sub(" ", str(value))


def redact_identifier(value: Any, length: int = 12) -> str:
    """
    Produce a bounded, non-reversible correlation token for an identifier.

    Use for values that must stay stable across log lines for correlation
    (actor/subject, session/thread id) but must never appear in plaintext. The
    token is a keyed HMAC-SHA-256 prefix over a per-process random key, so it is
    not a plain reversible hash of a guessable value and cannot be joined across
    processes, restarts, or tiers. Prefer request IDs where correlation does not
    require tying log lines to a specific principal.

    Args:
        value: Identifier to redact (may be None/empty).
        length: Number of hex characters to keep, clamped to a bounded range so
            the full digest can never be emitted and the token stays
            distinguishing.

    Returns:
        A short ``id:<hex>`` token, or ``<none>`` for empty values.
    """
    text = "" if value is None else str(value)
    if not text:
        return "<none>"
    clamped = max(_REDACT_MIN_LENGTH, min(_REDACT_MAX_LENGTH, length))
    # "backslashreplace" keeps this total: request-supplied text can hold lone
    # surrogates that strict UTF-8 encoding rejects, and a log helper must not
    # fail the request. Each such code point becomes a distinct escape, so
    # different inputs still get different tokens.
    digest = hmac.new(_LOG_REDACTION_KEY, text.encode("utf-8", "backslashreplace"), hashlib.sha256).hexdigest()
    return f"id:{digest[:clamped]}"


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

    # Normalize control characters so a redacted value cannot inject log lines.
    text = normalize_log_value(text)

    # Truncate if too long
    if len(text) > max_length:
        text = text[:max_length] + "..."

    return text


# Typed, sanitized error codes. These are stable identifiers safe to log; they
# carry no provider-authored text, ARNs, caller values, or network coordinates.
ERROR_ACCESS_DENIED = "access_denied"
ERROR_NOT_FOUND = "not_found"
ERROR_THROTTLED = "throttled"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_PROVIDER_ERROR = "provider_error"
ERROR_TIMEOUT = "timeout"

_DENIED_CODES = {
    "AccessDeniedException",
    "AccessDenied",
    "UnauthorizedException",
    "UnauthorizedOperation",
    "NotAuthorized",
    "ForbiddenException",
}
_NOT_FOUND_CODES = {"NotFoundException", "ResourceNotFoundException", "ValidationException_NotFound"}
_THROTTLE_CODES = {
    "ThrottlingException",
    "Throttling",
    "TooManyRequestsException",
    "LimitExceededException",
    "ServiceQuotaExceededException",
}
_INVALID_CODES = {"InvalidRequestException", "ValidationException", "InvalidParameterException"}


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a typed, sanitized error code.

    The raw message is never inspected for content that is returned; only a
    small, code-owned vocabulary crosses into the log. ``botocore`` client
    errors are mapped from their service error code; everything else falls back
    to a generic provider/timeout code. This keeps the operationally useful
    signal (denied vs throttled vs invalid vs timeout vs other) without the
    provider- or caller-authored message string.
    """
    if isinstance(exc, TimeoutError):
        return ERROR_TIMEOUT
    # Resolve a botocore ClientError's service code without importing botocore at
    # module import time (keeps this helper usable in minimal contexts).
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "") if isinstance(response.get("Error"), dict) else ""
        if code in _DENIED_CODES:
            return ERROR_ACCESS_DENIED
        if code in _NOT_FOUND_CODES:
            return ERROR_NOT_FOUND
        if code in _THROTTLE_CODES:
            return ERROR_THROTTLED
        if code in _INVALID_CODES:
            return ERROR_INVALID_REQUEST
        if code:
            return ERROR_PROVIDER_ERROR
    return ERROR_PROVIDER_ERROR


# Upper bound on the number of raise-site frames recorded on the production
# error path. The innermost frames are the most useful for triage; bounding the
# count keeps a deep or recursive stack from producing an unbounded log line.
_MAX_TRACEBACK_FRAMES = 20

# Upper bound on the length of each component (module and function name) in a
# single frame locus. A caller or dynamically generated frame can carry an
# arbitrarily long ``__name__`` or ``co_name``; capping each component keeps a
# single frame from producing an unbounded log line.
_MAX_FRAME_COMPONENT_LENGTH = 128

# Constant used in place of a frame's module name when ``__name__`` is missing
# or is not a ``str``. A code-owned sentinel is used rather than a filesystem
# path so a frame locus can never disclose ``co_filename``.
_UNKNOWN_FRAME_MODULE = "<unknown>"


def _frame_component(value: Any) -> str:
    """Normalize and bound-cap a single frame component.

    The value is collapsed to a single injection-safe line, stripped of lone
    surrogate code points so a strict UTF-8 sink can always encode the line, and
    truncated to :data:`_MAX_FRAME_COMPONENT_LENGTH` characters so no single
    component can forge a log line or grow without bound.
    """
    text = normalize_log_value(value)
    # A frame's ``__name__`` or ``co_name`` can hold lone surrogate code points
    # that a strict UTF-8 sink rejects with ``UnicodeEncodeError``; such a sink
    # failure during a log call made inside an ``except`` block drives Loguru's
    # handler-error path, which prints the in-flight exception to stderr. Replace
    # each lone surrogate with its backslash escape so the component is always
    # encodable while staying distinguishing.
    text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    if len(text) > _MAX_FRAME_COMPONENT_LENGTH:
        text = text[:_MAX_FRAME_COMPONENT_LENGTH]
    return text


def sanitized_traceback_frames(exc: BaseException, *, limit: int = _MAX_TRACEBACK_FRAMES) -> list[str]:
    """Return a bounded list of ``module:function:lineno`` raise-site frames.

    Each frame contributes only three code-owned values: the dotted module name
    (``frame.f_globals["__name__"]`` when it is a string, which carries no
    filesystem path), the function name (``frame.f_code.co_name``), and the line
    number. The exception message, local variable values, call arguments, and
    source text are never read, so a provider- or caller-authored string cannot
    ride along. When a frame's ``__name__`` is missing or is not a string, a
    code-owned ``<unknown>`` sentinel is used rather than ``co_filename``, so a
    frame locus can never disclose a file path. Each component is passed through
    :func:`normalize_log_value` and length-capped, so an exotic ``__name__`` or
    ``co_name`` holding control characters or an unbounded length cannot forge a
    log line. The innermost ``limit`` frames are kept (the raise site and its
    nearest callers), which is where triage starts.

    The walk is wrapped so the helper is total: it never raises, even for a frame
    whose ``__name__`` is an object whose ``__str__`` raises or a component that
    cannot be normalized. On any failure it returns the frames gathered so far,
    innermost first, so a sanitized error record is always produced.

    Args:
        exc: The exception whose traceback frames are summarized.
        limit: Maximum number of frames to return, from the innermost outward.

    Returns:
        A list of ``module:function:lineno`` strings, innermost first. Empty when
        the exception carries no traceback.
    """
    frames: list[str] = []
    try:
        tb = exc.__traceback__
        # Walk outermost-to-innermost, collecting a compact locus per frame, then
        # reverse so the innermost (raise site) comes first and the bound keeps
        # the frames closest to the failure.
        while tb is not None:
            frame = tb.tb_frame
            raw_module = frame.f_globals.get("__name__")
            # Use the module name only when it is genuinely a ``str`` (an exact
            # type check, so a ``str`` subclass cannot supply its own ``__str__``
            # text). A non-string (or missing) ``__name__`` resolves to a
            # code-owned sentinel rather than a file path, so ``co_filename``
            # never reaches the log.
            module = _frame_component(raw_module) if type(raw_module) is str else _UNKNOWN_FRAME_MODULE
            function = _frame_component(frame.f_code.co_name)
            lineno = tb.tb_lineno
            frames.append(f"{module}:{function}:{lineno}")
            tb = tb.tb_next
        frames.reverse()
    except Exception:
        # Keep the helper total: a sanitized error record must still be produced
        # even if an exotic frame makes the walk raise. Reverse the frames
        # gathered so far so the result stays innermost-first, matching the
        # success path, and return that bounded, content-free list.
        frames.reverse()
    clamped = max(1, int(limit)) if isinstance(limit, int) else _MAX_TRACEBACK_FRAMES
    return frames[:clamped]


def log_sanitized_exception(
    log: Any,
    message: str,
    exc: BaseException,
    *,
    request_id: str | None = None,
    debug_traceback: bool | None = None,
) -> None:
    """Log a bounded, non-sensitive record of an exception.

    The exception class name, a typed sanitized error code, the
    request-correlation ID (when available), and a bounded list of raise-site
    frames (``module:function:lineno``) are logged. The raw exception message is
    **never** emitted on the production path: a provider validation message or
    SDK error body can quote caller-supplied values (prompts, actor/session IDs,
    namespaces) and may itself carry CR/LF that would forge a second log line.
    The frame list carries only code-owned values — module name, function name,
    and line number — and never the message, local variable values, or call
    arguments, so it restores raise-site triage without reintroducing content.
    The exception object is deliberately not attached on this path, because
    Loguru's traceback rendering serializes ``str(exc)`` into the sink — the
    exact disclosure this function prevents.

    The full, framework-rendered traceback is emitted only when debug logging is
    explicitly enabled (``settings.ENABLE_DEBUG_LOGGING``), a dev-only switch; a
    caller may override with ``debug_traceback`` for testing.

    Args:
        log: A Loguru logger (or compatible) with ``bind`` and ``error``.
        message: A constant, code-owned message. Must not interpolate caller or
            provider values.
        exc: The exception to classify and record.
        request_id: Optional bounded correlation ID (safe, non-reversible or
            server-generated) to tie the record to a request. When ``None``, the
            request ID bound for the current invocation is used so a swallowed
            error still correlates to its request.
        debug_traceback: Force the full rendered traceback on/off; defaults to
            the debug-logging setting. The bounded frame locus is always
            recorded regardless of this flag.
    """
    if debug_traceback is None:
        try:
            # Local modules
            from config import settings

            debug_traceback = bool(getattr(settings, "ENABLE_DEBUG_LOGGING", False))
        except Exception:
            debug_traceback = False

    error_class = type(exc).__name__
    error_code = classify_exception(exc)

    # Resolve the correlation ID. When the caller does not pass one explicitly,
    # fall back to the request ID bound for the current invocation so a swallowed
    # error (memory-setup fallback, extraction-skipped, specialist fallback, or
    # a semantic-memory save failure) is still tied to its request. Only when no
    # request is in scope does the record read ``<none>``.
    if request_id is None:
        try:
            # Local modules
            from utils.logger import _REQUEST_ID_VAR

            request_id = _REQUEST_ID_VAR.get()
        except Exception:
            request_id = None
    rid = request_id if request_id else "<none>"

    # Compose the correlation fields into the message so they appear in the
    # human-readable stdout format (which renders {message}); also bind them as
    # structured extras for structured sinks. The class name is a Python type
    # name and the code is from a fixed vocabulary — neither carries caller or
    # provider text. The bounded raise-site frames are code-owned loci only.
    frames = sanitized_traceback_frames(exc)
    frames_text = " ".join(frames) if frames else "<none>"
    composed = f"{message} [class={error_class} code={error_code} request_id={rid} frames={frames_text}]"
    bound = log.bind(error_class=error_class, error_code=error_code, request_id=rid, frames=frames)
    # ``opt(depth=1)`` makes Loguru attribute the record to this helper's caller
    # (the error site in the orchestrator/specialist/entrypoint), not to this
    # helper, so ``{name}:{function}:{line}`` points back to the source.
    if debug_traceback:
        bound.opt(depth=1, exception=exc).error(composed)
    else:
        bound.opt(depth=1).error(composed)


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
