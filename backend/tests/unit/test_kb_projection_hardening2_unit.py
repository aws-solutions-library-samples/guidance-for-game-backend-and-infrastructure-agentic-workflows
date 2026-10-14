#!/usr/bin/env python3
"""Second-pass adversarial hardening regressions for KB output projection (#463).

Each test here was written to FAIL (RED) against the second-pass #463
implementation at a specific assertion, then made green by the corrective pass.
They cover the orchestrator's confirmed residual findings that the first
hardening pass did not fully close:

1. ``_structured_error_code`` must extract defensively (no throw when
   ``response['Error']`` is not a mapping) and the retry/failure logs must
   NEVER print the raw provider error code — only a fixed classification.
2. Retry fan-out and backoff delay must be clamped by code-owned hard maxima,
   safe under NaN / negative / huge ``RETRY_MAX_ATTEMPTS`` / ``RETRY_BASE_DELAY``.
3. When visible content is redacted (not merely truncated), the envelope must
   carry a truthful ``textRedacted`` / ``metadataRedacted`` signal and the
   primary state must not remain ``complete``.
4. Visible-string patterns must reject any 12-digit substring (including inside
   a longer digit run), C1 controls, credential/Bearer/Basic markers,
   certificate markers, IPv6, host:port, and padded / noncanonical dotted
   numeric runs — without logging or returning the raw hostile value.
5. ``project_error`` must never echo an unknown caller-provided error code.
6. A present-but-invalid score (non-finite / wrong type) or a required text row
   of the wrong type must make the outcome non-complete, not silently complete.
7. Query must be bounded by a code-owned UTF-8 byte cap with boundary-safe
   clipping, and the bounded query is the cache identity.
8. Cache TTL / max-entry env values must be hard-bounded; malformed config must
   not crash import or allocate an unreviewed cache size.
9. The unused doc-label limit must not be surfaced in public diagnostics.

Provider boundary is mocked with synthetic, public-safe fixtures only.
"""

# Standard library
import importlib
import io
import json
import math
import os
import re
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
def _raw_result(*, text: str, score: Any = 0.9, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
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
    # Local modules
    from utils.logger import logger

    buffer = io.StringIO()
    sink_id = logger.add(buffer, level="DEBUG", format="{message}")
    try:
        yield buffer
    finally:
        logger.remove(sink_id)


def _client_error(code: str, message: str, *, error_obj: Any = None) -> Exception:
    """Build a boto-style ClientError-like exception with a .response dict.

    ``error_obj`` overrides the whole ``response['Error']`` value so we can
    exercise a malformed (non-mapping) nested response.
    """

    class _ClientError(Exception):
        pass

    exc = _ClientError(f"{code}: {message}")
    if error_obj is not None:
        exc.response = {"Error": error_obj}
    else:
        exc.response = {"Error": {"Code": code, "Message": message}}
    exc.operation_name = "Retrieve"
    return exc


# ---------------------------------------------------------------------------
# Finding 1 — defensive structured-code extraction + no raw apiCode in logs.
# ---------------------------------------------------------------------------
def test_structured_error_code_handles_non_mapping_error():
    # Local modules
    from utils.kb_tools import _structured_error_code

    # response['Error'] is a string, not a mapping. Must NOT raise.
    exc = _client_error("x", "y", error_obj="AccessDeniedException string form")
    assert _structured_error_code(exc) == ""  # extracted defensively, no throw

    # response['Error'] is a list.
    exc2 = _client_error("x", "y", error_obj=["not", "a", "map"])
    assert _structured_error_code(exc2) == ""

    # response itself is not a dict.
    class _E(Exception):
        pass

    e3 = _E("boom")
    e3.response = "totally-not-a-dict"
    assert _structured_error_code(e3) == ""


def test_hostile_error_code_not_logged_raw(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)

    # A malicious/malformed Error.Code carrying an ARN / account id / control
    # text. It is NOT a known retryable code, so a single failing attempt.
    hostile = "arn:aws:iam::123456789012:role/pwned\x1b[31m tenant=acme " + ("Z" * 500)
    exc = _client_error(hostile, "provider prose")
    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-gamelift")

    with _capture_logs() as buffer:
        with _provider(stub):
            out = _call_tool(tool, text="q", numberOfResults=1, score=0.5)

    logs = buffer.getvalue()
    assert "arn:aws:iam" not in logs
    assert "123456789012" not in logs
    assert "tenant=acme" not in logs
    assert "ZZZZZZ" not in logs  # unbounded provider code not echoed
    assert "\x1b" not in logs
    # Envelope stays code-owned.
    assert json.loads(out)["state"] in {"unavailable", "denied"}
    assert "arn:aws:iam" not in out


# ---------------------------------------------------------------------------
# Finding 2 — retry fan-out and delay are hard-bounded and NaN/huge-safe.
# ---------------------------------------------------------------------------
def test_retry_attempts_hard_bounded(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)
    # A hostile deployment sets an enormous attempt count.
    monkeypatch.setattr(kb_tools, "RETRY_MAX_ATTEMPTS", 10_000_000)

    exc = _client_error("ThrottlingException", "transient")
    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        _call_tool(tool, text="q", numberOfResults=1, score=0.5)

    # Attempts must be clamped to a small code-owned maximum, not 10 million.
    assert len(stub.calls) <= 10, f"retry fan-out not hard-bounded: {len(stub.calls)}"


def test_backoff_delay_is_finite_and_bounded_under_nan_and_huge():
    # Local modules
    import utils.kb_tools as kb_tools

    slept: List[float] = []
    with patch.object(kb_tools.time, "sleep", lambda d: slept.append(d)):
        for base in (float("nan"), float("inf"), -5.0, 1e18):
            with patch.object(kb_tools, "RETRY_BASE_DELAY", base):
                slept.clear()
                kb_tools._sleep_backoff(3)
                assert slept, "sleep not called"
                d = slept[0]
                assert math.isfinite(d), f"non-finite delay for base={base}: {d}"
                assert 0.0 <= d <= 60.0, f"delay out of bounds for base={base}: {d}"


# ---------------------------------------------------------------------------
# Finding 3 — redaction is signalled and does not stay "complete".
# ---------------------------------------------------------------------------
def test_redacted_chunk_is_not_reported_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    # Content whose only issue is a sensitive substring (not length): redaction
    # changes it, but it is neither truncated nor dropped.
    raw = {"retrievalResults": [_raw_result(text="See arn:aws:s3:::secret and acct 123456789012 done.")]}
    env = json.loads(project_retrieve_response(raw, eff))

    assert env.get("textRedacted") is True
    assert env["state"] != "complete"


def test_redacted_metadata_is_signalled():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    raw = {
        "retrievalResults": [_raw_result(text="clean prose with no secrets", metadata={"title": "acct 123456789012"})]
    }
    env = json.loads(project_retrieve_response(raw, eff))
    assert env.get("metadataRedacted") is True
    assert env["state"] != "complete"


def test_sanitize_visible_reports_change():
    # Local modules
    from utils.kb_projection import _sanitize_visible

    # New contract: returns (text, changed).
    clean, changed = _sanitize_visible("plain safe text", max_chars=100)
    assert changed is False
    assert clean == "plain safe text"

    red, changed2 = _sanitize_visible("acct 123456789012", max_chars=100)
    assert changed2 is True
    assert "123456789012" not in red


# ---------------------------------------------------------------------------
# Finding 4 — hardened visible-string patterns.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostile,forbidden",
    [
        # 12-digit run embedded inside a longer digit run.
        ("id999123456789012000tail", "123456789012"),
        # C1 control character.
        ("before\x85after", "\x85"),
        # credential assignment.
        ("api_key: sk-livesecrettoken12345", "sk-livesecrettoken12345"),
        # Bearer marker.
        ("Authorization Bearer abctoken.value.here", "abctoken.value.here"),
        # Basic marker.
        ("Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        # certificate marker.
        ("-----BEGIN CERTIFICATE-----MIIB", "BEGIN CERTIFICATE"),
        # IPv6.
        ("host fe80::1ff:fe23:4567:890a here", "fe80::1ff:fe23:4567:890a"),
        # host:port shape.
        ("connect db.internal.example:5432 now", "db.internal.example:5432"),
        # padded / noncanonical dotted numeric run.
        ("addr 010.001.000.005 padded", "010.001.000.005"),
    ],
)
def test_visible_patterns_reject_hostile_shapes(hostile, forbidden):
    # Local modules
    from utils.kb_projection import _sanitize_visible

    cleaned, changed = _sanitize_visible(hostile, max_chars=500)
    assert forbidden not in cleaned, f"leaked {forbidden!r} -> {cleaned!r}"
    assert changed is True


# ---------------------------------------------------------------------------
# Finding 5 — project_error never echoes an unknown code.
# ---------------------------------------------------------------------------
def test_project_error_normalizes_unknown_code():
    # Local modules
    from utils.kb_projection import KBErrorCode, project_error

    hostile = "arn:aws:iam::123456789012:role/x INJECTED"
    env = json.loads(project_error(hostile))
    assert env["errorCode"] != hostile
    assert env["errorCode"] in {
        KBErrorCode.DENIED,
        KBErrorCode.UNAVAILABLE,
        KBErrorCode.MALFORMED,
        KBErrorCode.INTERVENED,
    }
    assert "123456789012" not in json.dumps(env)
    assert "INJECTED" not in json.dumps(env)


# ---------------------------------------------------------------------------
# Finding 6 — invalid provider row shapes are truthfully non-complete.
# ---------------------------------------------------------------------------
def test_present_but_nonfinite_score_is_not_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    # A row that is otherwise usable but carries a present, non-finite score.
    raw = {"retrievalResults": [_raw_result(text="usable prose", score=float("nan"))]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["state"] != "complete"


def test_present_but_wrongtype_score_is_not_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    raw = {"retrievalResults": [_raw_result(text="usable prose", score="high")]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["state"] != "complete"


def test_optional_absent_metadata_and_location_is_still_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    # No metadata, no location, no score — all optional per the service model.
    raw = {"retrievalResults": [{"content": {"type": "TEXT", "text": "clean prose"}}]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["state"] == "complete"


# ---------------------------------------------------------------------------
# Finding 7 — query bounded by a code-owned UTF-8 byte cap, boundary-safe.
# ---------------------------------------------------------------------------
def test_query_is_byte_bounded_multibyte():
    # Local modules
    import utils.kb_projection as proj
    from utils.kb_projection import bound_query

    # Each CJK char is 3 UTF-8 bytes. A string under the CHAR cap can still be
    # far over any sane byte cap.
    q = "字" * 4000
    bounded = bound_query(q)
    # Must be bounded by a code-owned UTF-8 byte cap.
    assert hasattr(proj, "KB_QUERY_MAX_BYTES")
    assert len(bounded.encode("utf-8")) <= proj.KB_QUERY_MAX_BYTES
    # Boundary-safe: no partial/replacement bytes.
    assert "\ufffd" not in bounded
    # Round-trips cleanly.
    bounded.encode("utf-8").decode("utf-8")


# ---------------------------------------------------------------------------
# Finding 8 — cache TTL / max-entry env values are hard-bounded.
# ---------------------------------------------------------------------------
def test_malformed_cache_config_does_not_crash_import():
    # Local modules
    import utils.kb_tools as kb_tools

    with patch.dict(
        os.environ,
        {
            "GBAW_KB_CACHE_TTL_SECONDS": "-999",
            "GBAW_KB_CACHE_MAX_ENTRIES": "not-a-number",
        },
    ):
        importlib.reload(kb_tools)
        try:
            stats = kb_tools.get_kb_cache_stats()
            assert stats["max_entries"] >= 1
            assert stats["ttl_seconds"] >= 0
            # Not an unreviewed/huge allocation.
            assert stats["max_entries"] <= 100_000
        finally:
            importlib.reload(kb_tools)


def test_huge_cache_max_entries_is_clamped():
    # Local modules
    import utils.kb_tools as kb_tools

    with patch.dict(os.environ, {"GBAW_KB_CACHE_MAX_ENTRIES": "1" * 20}):
        importlib.reload(kb_tools)
        try:
            stats = kb_tools.get_kb_cache_stats()
            assert stats["max_entries"] <= 100_000
        finally:
            importlib.reload(kb_tools)


# ---------------------------------------------------------------------------
# Finding 9 — unused doc-label limit is not surfaced in public diagnostics.
# ---------------------------------------------------------------------------
def test_doc_label_limit_not_in_public_diagnostics():
    # Local modules
    from utils.kb_projection import get_projection_limits

    assert "doc_label_max_chars" not in get_projection_limits()


# ===========================================================================
# Third-pass regressions (#463) — verified residual line-level defects.
#
# Each test below was written to FAIL (RED) against the current code at a
# specific assertion, then made green by the corrective pass. They cover:
#
# D1. ``kb_projection.classify_provider_error`` extracted ``response['Error']``
#     with ``.get(...)`` and threw on a non-mapping Error value, escaping the
#     retry catch path and propagating out of the tool. The tool must instead
#     return a code-owned unavailable/denied envelope and must not throw or log
#     raw provider text.
# D2. The KB retry / failure logs interpolated ``type(exc).__name__`` with no
#     code-owned length/grammar bound, so a dynamically named hostile / oversized
#     exception class leaked into the logs. The type label must be normalized to
#     a short, bounded, safe token before logging.
# D3. PEM sanitization replaced the BEGIN/END markers separately and left the
#     certificate / private-key BODY (and an unterminated BEGIN body) visible.
#     The whole block, including the multiline body, must be redacted.
# D4. A row with ``content.type`` present and not ``TEXT`` but carrying a
#     ``text`` member was accepted as a complete row. Absent type is accepted
#     (the installed service model makes it optional); a PRESENT type must be
#     ``TEXT`` or the row is dropped and the outcome is truthfully non-complete.
# D5. Unused loop bookkeeping (``considered``, ``index``) and its stale comments
#     must be gone, with every existing bound/state/cache behavior preserved.
# ---------------------------------------------------------------------------


def _dynamic_exc_class(base_name: str, extra: str) -> type:
    """Build a dynamically named exception class (hostile / oversized name)."""
    return type(base_name + extra, (Exception,), {})


# ---------------------------------------------------------------------------
# D1 — classify_provider_error is defensive against a non-mapping Error value,
# exercised through the real tool call path.
# ---------------------------------------------------------------------------
def test_classify_provider_error_non_mapping_error_does_not_throw():
    # Local modules
    from utils.kb_projection import KBErrorCode, classify_provider_error

    for error_obj in ("AccessDeniedException string form", ["not", "a", "map"], 12345):
        exc = _client_error("x", "y", error_obj=error_obj)
        # Must not raise on a non-mapping Error value.
        code = classify_provider_error(exc)
        assert code in {KBErrorCode.DENIED, KBErrorCode.UNAVAILABLE}
        # A non-mapping Error yields no recognizable denied code -> unavailable.
        assert code == KBErrorCode.UNAVAILABLE


def test_tool_returns_code_owned_envelope_when_error_is_non_mapping_string(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)

    # response['Error'] is a raw string carrying an ARN / account id: the old
    # code did response['Error'].get('Code') and raised AttributeError here,
    # escaping the retry catch and propagating out of the tool.
    hostile = "arn:aws:iam::123456789012:role/pwned AccessDeniedException"
    exc = _client_error("ignored", "prose", error_obj=hostile)
    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-gamelift")

    with _capture_logs() as buffer:
        with _provider(stub):
            # Must NOT throw.
            out = _call_tool(tool, text="q", numberOfResults=1, score=0.5)

    env = json.loads(out)
    assert env["state"] in {"unavailable", "denied"}
    assert env.get("errorCode") in {"KB_UNAVAILABLE", "KB_ACCESS_DENIED"}
    # No raw provider text in the envelope or the logs.
    assert "arn:aws:iam" not in out
    assert "123456789012" not in out
    logs = buffer.getvalue()
    assert "arn:aws:iam" not in logs
    assert "123456789012" not in logs


def test_tool_returns_code_owned_envelope_when_error_is_list(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)

    exc = _client_error("ignored", "prose", error_obj=["AccessDeniedException", "x"])
    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-eks")
    with _provider(stub):
        out = _call_tool(tool, text="q", numberOfResults=1, score=0.5)
    env = json.loads(out)
    assert env["state"] in {"unavailable", "denied"}


# ---------------------------------------------------------------------------
# D2 — exception TYPE name is normalized to a short, bounded, safe label
# before it is logged.
# ---------------------------------------------------------------------------
def test_hostile_exception_type_name_not_logged_raw(monkeypatch):
    # Local modules
    import utils.kb_tools as kb_tools
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    monkeypatch.setattr(kb_tools.time, "sleep", lambda *_a, **_k: None)

    # A dynamically named exception whose class name is oversized and carries
    # control text. Known-retryable prefix is irrelevant: the NAME itself is the
    # payload that must not reach the logs verbatim.
    hostile_name = "Boom" + ("Q" * 500) + "\x1b[31m\n"
    exc = _dynamic_exc_class(hostile_name, "")("provider prose")
    exc.response = {"Error": {"Code": "SomethingUnavailable"}}
    exc.operation_name = "Retrieve"

    stub = _AgentRuntimeStub(exc=exc)
    tool = create_kb_retrieve_tool("kb-synth-cost")
    with _capture_logs() as buffer:
        with _provider(stub):
            out = _call_tool(tool, text="q", numberOfResults=1, score=0.5)

    logs = buffer.getvalue()
    # The oversized run and control bytes must not be interpolated raw.
    assert "QQQQQQ" not in logs
    assert "\x1b" not in logs
    assert "\n\x1b" not in logs
    # No individual log line carries an unbounded type token.
    for line in logs.splitlines():
        if "type=" in line:
            label = line.split("type=", 1)[1].split(" ")[0]
            assert len(label) <= 64, f"unbounded type label logged: {len(label)}"
    # Envelope stays code-owned.
    assert json.loads(out)["state"] in {"unavailable", "denied"}


def test_safe_type_label_is_short_and_grammar_bounded():
    # Local modules
    import utils.kb_tools as kb_tools

    # A code-owned normalizer must exist and bound both length and grammar.
    assert hasattr(kb_tools, "_safe_type_label"), "expected a code-owned _safe_type_label normalizer"

    # Known class passes through recognizably.
    assert kb_tools._safe_type_label(ValueError("x")) in {
        "ValueError",
        "other",
    } or "Value" in kb_tools._safe_type_label(ValueError("x"))

    hostile = _dynamic_exc_class("Evil" + ("Z" * 500), "\x1b bad name")("boom")
    label = kb_tools._safe_type_label(hostile)
    assert len(label) <= 64
    assert "\x1b" not in label
    assert "ZZZZZZ" not in label
    # Bounded identifier grammar (or the explicit "other" fallback).
    assert re.fullmatch(r"[A-Za-z0-9_]{1,64}", label), f"non-identifier label: {label!r}"


# ---------------------------------------------------------------------------
# D3 — the entire PEM block (including the multiline body) is redacted.
# ---------------------------------------------------------------------------
def test_pem_certificate_body_is_fully_redacted():
    # Local modules
    from utils.kb_projection import _sanitize_visible

    pem = (
        "prefix text "
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBODYSECRETLINEONE1234567890abcdef\n"
        "SECONDSECRETLINEbase64body==\n"
        "-----END CERTIFICATE-----"
        " trailing text"
    )
    cleaned, changed = _sanitize_visible(pem, max_chars=1000)
    assert changed is True
    assert "MIIBODYSECRETLINEONE" not in cleaned
    assert "SECONDSECRETLINEbase64body" not in cleaned
    assert "BEGIN CERTIFICATE" not in cleaned
    assert "END CERTIFICATE" not in cleaned


def test_pem_private_key_body_is_fully_redacted():
    # Local modules
    from utils.kb_projection import _sanitize_visible

    begin_marker = "-----BEGIN RSA " + "PRIVATE" + " KEY-----"
    end_marker = "-----END RSA " + "PRIVATE" + " KEY-----"
    pem = f"{begin_marker}\n" "PRIVATEKEYSECRETMATERIALLINE1\n" "PRIVATEKEYSECRETMATERIALLINE2\n" f"{end_marker}"
    cleaned, changed = _sanitize_visible(pem, max_chars=1000)
    assert changed is True
    assert "PRIVATEKEYSECRETMATERIALLINE1" not in cleaned
    assert "PRIVATEKEYSECRETMATERIALLINE2" not in cleaned
    assert "PRIVATE KEY" not in cleaned


def test_unterminated_pem_begin_body_is_redacted_conservatively():
    # Local modules
    from utils.kb_projection import _sanitize_visible

    # No END marker: a following bounded token/body must still be redacted.
    text = "-----BEGIN CERTIFICATE-----\nDANGLINGSECRETBODYtoken1234567890 tail"
    cleaned, changed = _sanitize_visible(text, max_chars=1000)
    assert changed is True
    assert "DANGLINGSECRETBODYtoken1234567890" not in cleaned
    assert "BEGIN CERTIFICATE" not in cleaned


def test_pem_body_not_surfaced_in_projected_chunk():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    pem_text = (
        "cert follows " "-----BEGIN CERTIFICATE-----\n" "PROJECTEDBODYSECRETmaterial9999\n" "-----END CERTIFICATE-----"
    )
    raw = {"retrievalResults": [_raw_result(text=pem_text)]}
    out = project_retrieve_response(raw, eff)
    assert "PROJECTEDBODYSECRETmaterial9999" not in out
    env = json.loads(out)
    assert env.get("textRedacted") is True
    assert env["state"] != "complete"


# ---------------------------------------------------------------------------
# D4 — content.type, when PRESENT, must be TEXT; absent type stays accepted.
# ---------------------------------------------------------------------------
def test_present_non_text_content_type_row_is_dropped():
    # Local modules
    from utils.kb_projection import _project_one_result

    row = {"content": {"type": "IMAGE", "text": "looks like text but type is IMAGE"}}
    assert _project_one_result(row) is None


def test_present_non_text_content_type_makes_outcome_non_complete():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    raw = {
        "retrievalResults": [
            {"content": {"type": "IMAGE", "text": "not real text"}},
        ]
    }
    env = json.loads(project_retrieve_response(raw, eff))
    # The only row was non-TEXT -> dropped -> nothing projected -> not complete.
    assert env["returnedCount"] == 0
    assert env["state"] != "complete"


def test_absent_content_type_with_text_is_accepted():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    # Installed service model makes content.type optional; absent type + text is
    # a usable TEXT row and stays complete.
    raw = {"retrievalResults": [{"content": {"text": "clean usable prose"}}]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["returnedCount"] == 1
    assert env["state"] == "complete"


def test_present_text_content_type_is_accepted():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.0)
    raw = {"retrievalResults": [{"content": {"type": "TEXT", "text": "clean usable prose"}}]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["returnedCount"] == 1
    assert env["state"] == "complete"


# ---------------------------------------------------------------------------
# D5 — unused loop bookkeeping is removed; behavior is preserved.
# ---------------------------------------------------------------------------
def test_projection_loop_has_no_unused_bookkeeping():
    # Standard library
    import inspect

    # Local modules
    import utils.kb_projection as proj

    src = inspect.getsource(proj.project_retrieve_response)
    assert "considered" not in src, "unused 'considered' bookkeeping still present"
    # 'index' from `for index, row in enumerate(...)` is unused; the loop should
    # iterate rows directly.
    assert "for index, row in enumerate" not in src


def test_local_truncation_behavior_preserved():
    # Local modules
    from utils.kb_projection import EffectiveRequest, project_retrieve_response

    # More received rows than the effective cap -> local (client-side) truncation
    # must still be reported truthfully after the loop cleanup.
    eff = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=2, min_score=0.0)
    raw = {"retrievalResults": [_raw_result(text=f"row {i} clean prose") for i in range(5)]}
    env = json.loads(project_retrieve_response(raw, eff))
    assert env["returnedCount"] == 2
    assert env["localTruncated"] is True
    assert env["resultCount"] == 5
    assert env["state"] != "complete"


# ---------------------------------------------------------------------------
# Independent-review regression: preserve benign credential terminology.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prose",
    [
        "Change your password regularly for good security hygiene.",
        "Configure the authorization policy for your fleet scaling role.",
        "Store the private key material in an approved secret manager.",
        "Rotate the API key according to your security policy.",
    ],
)
def test_credential_terms_without_assigned_values_preserve_benign_prose(prose):
    # Local modules
    from utils.kb_projection import _sanitize_visible

    sanitized, changed = _sanitize_visible(prose, max_chars=500)
    assert sanitized == prose
    assert changed is False


@pytest.mark.parametrize(
    "assignment,secret_value",
    [
        ("password=synthetic-secret-value", "synthetic-secret-value"),
        ("api key: synthetic-token-value", "synthetic-token-value"),
        ("authorization: Bearer synthetic-bearer-value", "synthetic-bearer-value"),
    ],
)
def test_credential_assignments_still_redact_values(assignment, secret_value):
    # Local modules
    from utils.kb_projection import _sanitize_visible

    sanitized, changed = _sanitize_visible(assignment, max_chars=500)
    assert secret_value not in sanitized
    assert changed is True
