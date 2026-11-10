"""Pseudonymous actor identifiers for AgentCore Memory.

AgentCore Memory takes an *actor ID* on every event call and uses it as the
long-term-memory namespace. When tracing is enabled on the memory resource, the
memory service also emits trace spans, and the actor ID is a span attribute on
them. Passing the raw Cognito subject would put a stable user identifier into
exported telemetry that the runtime does not control.

The runtime therefore gives the memory service an opaque digest of the verified
subject instead. The digest is:

* deterministic, so one user always maps to the same actor ID and keeps their
  memory across sessions and restarts;
* one-way, so the subject cannot be recovered from an actor ID that appears in a
  trace, a log line, or a memory-service console;
* domain-separated, so it never equals a digest of the same subject computed for
  another purpose.

This is pseudonymization, not anonymization: anyone who already knows a subject
can compute its actor ID. An operator can use that to locate or delete one user's
memory. Cognito subjects are random UUIDs, so an actor ID does not reveal which
user it belongs to without a candidate list of subjects.

Changing the domain string, or the hash, assigns every user a new actor ID and
makes the memory stored under the old IDs unreachable from the runtime.
"""

# Standard library
import hashlib

# Separates this digest from any other digest of the same subject. Part of the
# identifier contract: see the module docstring before changing it.
_ACTOR_ID_DOMAIN = "gbaw:agentcore-memory-actor:v1:"


def memory_actor_id(subject: str) -> str:
    """Return the AgentCore Memory actor ID for a verified subject.

    The subject is hashed exactly as given: no trimming and no case folding, so
    two subjects that differ only by whitespace or case get different actor IDs.

    Args:
        subject: The verified user subject (a string with a non-whitespace character).

    Returns:
        A 64-character lowercase hexadecimal digest. It satisfies the AgentCore
        actor-ID character set and length limits.

    Raises:
        ValueError: If ``subject`` is not a string, is empty or whitespace-only, or cannot be
            encoded as UTF-8. Callers disable memory for the request rather than
            fall back to a raw identifier. The error never carries the subject,
            and no encoding error is chained to it: a codec error keeps the whole
            text it failed on, which a logger could then print.
    """
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("memory actor subject must be a string with a non-whitespace character")
    try:
        encoded = (_ACTOR_ID_DOMAIN + subject).encode("utf-8")
    except UnicodeEncodeError:
        encoded = None
    if encoded is None:
        raise ValueError("memory actor subject must be encodable as UTF-8")
    return hashlib.sha256(encoded).hexdigest()


# Separates the session digest below from the actor digest and any other digest of
# the same value. Changing it moves runtime-session conversations to new memory
# sessions; their earlier short-term history becomes unreachable.
_SESSION_ID_DOMAIN = "gbaw:agentcore-memory-session:v1:"
_SESSION_ID_PREFIX = "rt-"


def memory_session_id_for_runtime_session(runtime_session_id: str) -> str:
    """Return the AgentCore Memory session ID for an AgentCore runtime session.

    The runtime uses this only when a caller names no conversation. The frontend
    sends its per-thread ID as ``user_context.session_id`` and ``thread_id``; other
    callers (direct API clients, evaluations, probes) get the session AgentCore
    assigned to the invocation. Without it, every such invocation of one user
    would share one memory conversation, and concurrent invocations would replay
    each other's unanswered questions (#743).

    The ID is:

    * deterministic, so repeat invocations in one runtime session keep one
      conversation;
    * one-way, because the raw runtime session ID is kept out of logs (#472) and
      the memory SDK logs the session ID it is given;
    * domain-separated from :func:`memory_actor_id`;
    * within the memory service's session-ID rules: it starts with a letter and
      has 67 characters drawn from letters, digits, and hyphens.

    Args:
        runtime_session_id: The AgentCore runtime session ID of the invocation.

    Returns:
        ``"rt-"`` followed by a 64-character lowercase hexadecimal digest.

    Raises:
        ValueError: If ``runtime_session_id`` is not a string with a
            non-whitespace character or cannot be encoded as UTF-8. The error
            never carries the value.
    """
    if not isinstance(runtime_session_id, str) or not runtime_session_id.strip():
        raise ValueError("runtime session ID must be a string with a non-whitespace character")
    try:
        encoded = (_SESSION_ID_DOMAIN + runtime_session_id).encode("utf-8")
    except UnicodeEncodeError:
        encoded = None
    if encoded is None:
        raise ValueError("runtime session ID must be encodable as UTF-8")
    return _SESSION_ID_PREFIX + hashlib.sha256(encoded).hexdigest()
