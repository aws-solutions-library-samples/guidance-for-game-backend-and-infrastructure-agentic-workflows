#!/usr/bin/env python3
"""Adversarial hardening regressions for the KB output projection (#463).

Each test here was written to FAIL against the first-pass #463 implementation
at a specific assertion, then made green by the correction. They cover, in
order, the orchestrator-confirmed findings:

1. Retryable provider exception text/ARNs must not reach the logs (KB-owned
   bounded retry that logs only attempt#, exception type, sanitized class).
2. Field-value sanitization: allowlisted metadata VALUES and chunk text must
   reject/redact control chars, ARNs, URL schemes, 12-digit account ids,
   credential/cert markers, and network coordinates — while keeping prose.
3. Non-finite numeric handling: NaN/Infinity scores rejected; JSON serialized
   with allow_nan=False; bool not numeric.
4. Provider over-return: more rows than the effective count must be reported
   truthfully (known received count) and marked as local truncation.
5. Overlapping truths need independent signals (partial + aggregate trim, etc.).
6. guardrailAction=INTERVENED -> code-owned denied; malformed nextToken/action
   -> malformed, never complete.
7. _bounded_int must not log raw env values; effective limits stay hard-bounded
   even under inconsistent min/max overrides.
8. Source location stays coarse (sourceType only); docstring is truthful.
9. Unavailable/denied/malformed/intervened outcomes are NOT cached.
10. Exact UTF-8 aggregate bytes; all three specialist KB tools; cache
    count/query normalization; provider over-return.

Provider boundary is mocked with synthetic, public-safe fixtures only.
"""

# Standard library
import importlib
import io
import json
import math
import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, List
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Synthetic, public-safe provider fixtures.
# ---------------------------------------------------------------------------
def _raw_result(*, text: str, score: float = 0.9, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "content": {"type": "TEXT", "text": text},
        "location": {"type": "S3", "s3Location": {"uri": "s3://synthetic-bucket/guides/x.md"}},
        "metadata": metadata or {"title": "Scaling guide"},
        "score": score,
    }


class _AgentRuntimeStub:
    def __init__(self, response: Dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.calls: List[Dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


@contextmanager
def _provider(stub: _AgentRuntimeStub):
    # Local modules
    import utils.kb_tools as kb_tools

    with patch.object(kb_tools, "_build_agent_runtime_client", return_value=stub):
        yield stub


def _call_tool(tool, **kwargs) -> str:
    fn = getattr(tool, "__wrapped__", None) or getattr(tool, "_tool_func")
    return str(fn(**kwargs))


@contextmanager
def _capture_logs():
    """Capture everything the code logs via loguru into a buffer."""
    # Local modules
    from utils.logger import logger

    buffer = io.StringIO()
    sink_id = logger.add(buffer, level="DEBUG", format="{message}")
    try:
        yield buffer
    finally:
        logger.remove(sink_id)


def _client_error(code: str, message: str) -> Exception:
    """Build a boto-style ClientError-like exception with a .response dict."""

    class _ClientError(Exception):
        pass

    exc = _ClientError(f"{code}: {message}")
    exc.response = {"Error": {"Code": code, "Message": message}}
    exc.operation_name = "Retrieve"
    return exc


# ---------------------------------------------------------------------------
# Finding 1 — retryable exception text/ARNs must never reach the logs.
# ---------------------------------------------------------------------------
def test_retryable_exception_text_not_logged(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    # Avoid real sleeps between retries.
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)

    secret = "arn:aws:bedrock:us-west-2:123456789012:knowledge-base/SECRETKB tenant=acme"
    exc = _client_error("ThrottlingException", secret)
    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-gamelift")

    with _capture_logs() as buffer:
        with _provider(stub):
            out = _call_tool(tool, text="scaling", numberOfResults=1, score=0.5)

    logs = buffer.getvalue()
    # The retry path must have run but never logged provider text/ARNs — and,
    # per #463 F1, must not log the raw provider error code either.
    assert secret not in logs
    assert "arn:aws:bedrock" not in logs
    assert "123456789012" not in logs
    assert "tenant=acme" not in logs
    assert "ThrottlingException" not in logs  # raw provider error code is NOT logged
    # Only code-owned fields appear: attempt counter and sanitized classification.
    assert "classified=KB_UNAVAILABLE" in logs
    assert "attempt=" in logs
    # And the model boundary is a code-owned unavailable envelope.
    assert secret not in out
    assert json.loads(out)["state"] == "unavailable"


# ---------------------------------------------------------------------------
# Finding 2 — allowlisted metadata VALUES and chunk text are sanitized.
# ---------------------------------------------------------------------------
def test_allowlisted_metadata_value_is_sanitized():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    # "title" is an allowed KEY, but its VALUE is adversarial.
    private_key_marker = "-----BEGIN " + "PRIVATE" + " KEY-----"
    nasty_title = (
        "Guide arn:aws:s3:::secret-bucket/prod acct 123456789012 "
        "https://internal.example.com/x?token=abc 10.0.0.5:8080 "
        f"{private_key_marker} aws_secret_access_key=AKIAIOSFODNN7EXAMPLE"
    )
    stub = _AgentRuntimeStub({"retrievalResults": [_raw_result(text="ok", metadata={"title": nasty_title})]})
    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        out = _call_tool(tool, text="scaling", numberOfResults=1, score=0.5)

    for forbidden in (
        "arn:aws:s3",
        "123456789012",
        "https://internal.example.com",
        "10.0.0.5",
        "BEGIN PRIVATE KEY",
        "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key",
    ):
        assert forbidden not in out, forbidden
    # Useful prose survives.
    assert "Guide" in out


def test_chunk_text_sanitizes_unsafe_substrings_but_keeps_prose():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    body = (
        "To scale your fleet, set target utilization.\x07\x00 See "
        "arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-abc and "
        "s3://prod-runbooks/deploy-9f3c/secret.md at 192.168.1.10:22 "
        "authorization: Bearer supersecrettoken"
    )
    stub = _AgentRuntimeStub({"retrievalResults": [_raw_result(text=body)]})
    tool = create_kb_retrieve_tool("kb-synth-eks")
    with _provider(stub):
        out = _call_tool(tool, text="scale", numberOfResults=1, score=0.5)

    for forbidden in (
        "arn:aws:gamelift",
        "123456789012",
        "s3://prod-runbooks",
        "deploy-9f3c",
        "192.168.1.10",
        "supersecrettoken",
        "\x07",
        "\x00",
    ):
        assert forbidden not in out, forbidden
    # The benign guidance text is preserved.
    assert "To scale your fleet, set target utilization." in out


# ---------------------------------------------------------------------------
# Finding 3 — non-finite numeric handling.
# ---------------------------------------------------------------------------
def test_nan_and_infinity_scores_are_rejected_and_json_is_standard():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    raw = {
        "retrievalResults": [
            _raw_result(text="nan score", score=float("nan")),
            _raw_result(text="inf score", score=float("inf")),
            _raw_result(text="finite", score=0.7),
        ]
    }
    out = project_retrieve_response(raw, eff)
    # Standard JSON only — NaN/Infinity would raise or appear as bare tokens.
    assert "NaN" not in out
    assert "Infinity" not in out
    env = json.loads(out)  # would raise if non-standard tokens were emitted
    for item in env["results"]:
        assert item["score"] is None or math.isfinite(item["score"])


def test_resolve_score_rejects_nan_and_bool():
    # Local modules
    from utils.kb_tools import _resolve_score

    assert _resolve_score(float("nan")) == 0.5
    assert _resolve_score(float("inf")) == 0.5
    # bool must NOT be treated as numeric 0/1.
    assert _resolve_score(True) == 0.5
    assert _resolve_score(False) == 0.5


def test_bool_metadata_value_not_treated_as_number_shape():
    # Local modules
    from utils.kb_projection import _project_metadata

    projected, _redacted = _project_metadata({"title": True, "service": 0.5})
    assert projected["title"] in {"true", "false"}


# ---------------------------------------------------------------------------
# Finding 4 — provider over-return is truthful + marked as local truncation.
# ---------------------------------------------------------------------------
def test_provider_over_return_is_not_reported_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=2, min_score=0.0)
    raw = {"retrievalResults": [_raw_result(text=f"row {i}") for i in range(6)]}  # over-returns
    env = json.loads(project_retrieve_response(raw, eff))

    assert env["returnedCount"] == 2
    # Known received count is len() of the received list.
    assert env["resultCount"] == 6
    assert env["state"] != "complete"
    assert env["localTruncated"] is True


# ---------------------------------------------------------------------------
# Finding 5 — overlapping truths need independent signals.
# ---------------------------------------------------------------------------
def test_partial_and_aggregate_trim_both_visible(monkeypatch):
    # Local modules
    import utils.kb_projection as proj
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    # Force a tiny envelope cap so aggregate trimming fires, and a continuation
    # token so the state is already PARTIAL. Both truths must remain visible.
    monkeypatch.setattr(proj, "KB_ENVELOPE_MAX_BYTES", 400)
    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=10, min_score=0.0)
    raw = {
        "retrievalResults": [_raw_result(text="X" * 500) for _ in range(6)],
        "nextToken": "more",
    }
    env = json.loads(project_retrieve_response(raw, eff))
    # Continuation is not lost by the aggregate trim.
    assert env["state"] == "partial"
    assert env["hasMore"] is True
    # Aggregate trimming is independently visible.
    assert env.get("aggregateTruncated") is True


def test_all_present_rows_filtered_is_not_empty():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.99)
    raw = {"retrievalResults": [_raw_result(text="low", score=0.10)]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["returnedCount"] == 0
    assert env["resultCount"] == 1
    assert env["state"] != "empty"
    assert env["state"] != "complete"


# ---------------------------------------------------------------------------
# Finding 6 — guardrailAction handling + malformed shapes.
# ---------------------------------------------------------------------------
def test_guardrail_intervened_is_denied_without_provider_text():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=3, min_score=0.5)
    raw = {
        "guardrailAction": "INTERVENED",
        "retrievalResults": [_raw_result(text="blocked content that must not surface")],
    }
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["state"] not in {"complete", "empty"}
    assert env["state"] == "denied"
    assert env.get("guardrailIntervened") is True
    assert "blocked content" not in json.dumps(env)
    assert env["returnedCount"] == 0


def test_unexpected_guardrail_action_is_malformed():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=3, min_score=0.5)
    env = json.loads(project_retrieve_response({"guardrailAction": "WEIRD", "retrievalResults": []}, eff))
    assert env["state"] == "malformed"


def test_malformed_next_token_shape_is_not_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=3, min_score=0.5)
    raw = {"retrievalResults": [_raw_result(text="ok")], "nextToken": {"unexpected": "shape"}}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["state"] not in {"complete", "empty"}


# ---------------------------------------------------------------------------
# Finding 7 — _bounded_int must not log raw env values; hard bounds hold.
# ---------------------------------------------------------------------------
def test_bounded_int_does_not_log_raw_env_value():
    # Local modules
    import utils.kb_projection as proj

    with _capture_logs() as buffer:
        val = proj._bounded_int("GBAW_FAKE_LIMIT_XYZ_UNSET", default=7, minimum=1, maximum=10)
    assert val == 7

    injected = "9999999-INJECTED-SENTINEL-arn:aws:iam::123456789012:role/x"
    with patch.dict(os.environ, {"GBAW_FAKE_LIMIT_XYZ": injected}):
        with _capture_logs() as buffer2:
            val2 = proj._bounded_int("GBAW_FAKE_LIMIT_XYZ", default=3, minimum=1, maximum=10)
    assert val2 == 3
    logs = buffer2.getvalue()
    assert "INJECTED-SENTINEL" not in logs
    assert "123456789012" not in logs


def test_bounded_int_hard_bounds_under_inconsistent_overrides():
    # Local modules
    import utils.kb_projection as proj

    # minimum > maximum: the maximum must remain the hard ceiling.
    with patch.dict(os.environ, {"GBAW_FAKE_LIMIT_INCONSISTENT": "999999"}):
        val = proj._bounded_int("GBAW_FAKE_LIMIT_INCONSISTENT", default=5, minimum=100, maximum=10)
    assert val <= 10


# ---------------------------------------------------------------------------
# Finding 8 — source location stays coarse; docstring truthful.
# ---------------------------------------------------------------------------
def test_source_location_is_coarse_type_only():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=1, min_score=0.0)
    raw = {
        "retrievalResults": [
            {
                "content": {"type": "TEXT", "text": "content"},
                "location": {
                    "type": "S3",
                    "s3Location": {"uri": "s3://secret-bucket/prod/deploy-9f3c/runbook.md"},
                },
                "documentId": "doc-secret-777",
                "metadata": {"title": "ok"},
                "score": 0.9,
            }
        ]
    }
    out = project_retrieve_response(raw, eff)
    env = json.loads(out)
    item = env["results"][0]
    assert item["sourceType"] == "S3"
    # No basename label / documentId / uri / query is surfaced anywhere.
    assert "runbook.md" not in out
    assert "doc-secret-777" not in out
    assert "secret-bucket" not in out
    assert "docLabel" not in item and "documentId" not in item


def test_module_docstring_does_not_claim_basename_label():
    # Local modules
    import utils.kb_projection as proj

    doc = proj.__doc__ or ""
    # The first-pass docstring claimed a "basename-only document label" is
    # surfaced; the implementation only surfaces sourceType.
    assert "basename" not in doc.lower()


# ---------------------------------------------------------------------------
# Finding 9 — error/denied/malformed outcomes are NOT cached.
# ---------------------------------------------------------------------------
def test_unavailable_outcome_is_not_cached(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)

    exc = _client_error("ThrottlingException", "transient")
    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        first = _call_tool(tool, text="q", numberOfResults=1, score=0.5)
    assert json.loads(first)["state"] == "unavailable"

    # A subsequent success for the SAME request must NOT be shadowed by a cached
    # transient failure.
    stub_ok = _AgentRuntimeStub({"retrievalResults": [_raw_result(text="now works")]})
    with _provider(stub_ok):
        second = _call_tool(tool, text="q", numberOfResults=1, score=0.5)
    assert json.loads(second)["state"] in {"complete", "truncated", "partial", "empty"}
    assert len(stub_ok.calls) == 1  # provider re-queried, not served stale error


def test_denied_outcome_is_not_cached():
    # Local modules
    from utils.kb_projection import KBErrorCode, is_cacheable_envelope, project_error

    assert is_cacheable_envelope(project_error(KBErrorCode.DENIED)) is False
    assert is_cacheable_envelope(project_error(KBErrorCode.UNAVAILABLE)) is False
    assert is_cacheable_envelope(project_error(KBErrorCode.MALFORMED)) is False
    assert is_cacheable_envelope(project_error(KBErrorCode.INTERVENED)) is False


# ---------------------------------------------------------------------------
# Finding 10 — exact UTF-8 aggregate bytes, all three specialist KB tools,
# cache normalization.
# ---------------------------------------------------------------------------
def test_aggregate_envelope_never_exceeds_byte_cap(monkeypatch):
    # Local modules
    import utils.kb_projection as proj
    from utils.kb_projection import EffectiveRequest, get_projection_limits, project_retrieve_response

    monkeypatch.setattr(proj, "KB_ENVELOPE_MAX_BYTES", 1500)
    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=10, min_score=0.0)
    # Multibyte content to exercise exact UTF-8 byte accounting.
    raw = {"retrievalResults": [_raw_result(text="日本語テキスト " * 300) for _ in range(8)]}
    out = project_retrieve_response(raw, eff)
    assert len(out.encode("utf-8")) <= 1500


@pytest.mark.parametrize("kb_id", ["kb-gamelift-synth", "kb-eks-synth", "kb-cost-synth"])
def test_all_specialist_kb_tools_project_and_bind_id(kb_id):
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    stub = _AgentRuntimeStub({"retrievalResults": [_raw_result(text="doc")]})
    tool = create_kb_retrieve_tool(kb_id)
    with _provider(stub):
        out = _call_tool(tool, text="q", numberOfResults=1, score=0.5)
    assert stub.calls[0]["knowledgeBaseId"] == kb_id
    env = json.loads(out)
    assert env["schemaVersion"] == "kb-projection-v1"


def test_cache_key_normalizes_query_and_count():
    # Local modules
    from utils.kb_projection import bound_query
    from utils.kb_tools import EffectiveRequest, _get_cache_key

    # Query bounding is applied before the cache identity is computed.
    a = EffectiveRequest(
        kb_id="kb", region="us-west-2", query=bound_query("  spend  "), number_of_results=3, min_score=0.5
    )
    b = EffectiveRequest(kb_id="kb", region="us-west-2", query=bound_query("spend"), number_of_results=3, min_score=0.5)
    assert _get_cache_key(a) == _get_cache_key(b)
    # Distinct counts do not collide.
    c = EffectiveRequest(kb_id="kb", region="us-west-2", query="spend", number_of_results=5, min_score=0.5)
    d = EffectiveRequest(kb_id="kb", region="us-west-2", query="spend", number_of_results=3, min_score=0.5)
    assert _get_cache_key(c) != _get_cache_key(d)
