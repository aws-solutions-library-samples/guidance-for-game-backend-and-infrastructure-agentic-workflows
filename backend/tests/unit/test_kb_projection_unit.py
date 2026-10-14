#!/usr/bin/env python3
"""Unit regressions for the Knowledge Base model-facing output projection (#463).

These tests exercise the existing public API of ``utils.kb_tools`` — the tool
returned by ``create_kb_retrieve_tool`` — at the model-facing return boundary
(the string a specialist agent, and therefore the model, sees).

They assert that a raw/unbounded provider response, sensitive
metadata/location, a misleading completion state, and provider-authored
exception text MUST NOT reach that boundary.

The provider boundary is mocked with synthetic, public-safe fixtures only. To
stay valid across the pre-fix implementation (which delegates to
``strands_tools.retrieve``) and the post-fix implementation (which calls the
Bedrock Agent Runtime client directly), the fixtures install a stub at the
real AWS boundary — ``boto3.client("bedrock-agent-runtime")`` — so whichever
code path runs sees the same synthetic raw response.
"""

# Standard library
import json
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
# Synthetic, public-safe raw provider responses (Bedrock Agent Runtime
# Retrieve shape). No real ARNs, URIs, account IDs, or KB IDs.
# ---------------------------------------------------------------------------
def _raw_result(
    *,
    text: str,
    score: float = 0.9,
    s3_uri: str = "s3://synthetic-doc-bucket/guides/gamelift-scaling.md",
    extra_metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    metadata = {
        "x-amz-bedrock-kb-source-uri": s3_uri,
        "x-amz-bedrock-kb-chunk-id": "chunk-000123",
        "x-amz-bedrock-kb-data-source-id": "ds-synthetic-0001",
        "internal-owner-alias": "synthetic-team",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "content": {"type": "TEXT", "text": text},
        "documentId": "doc-synthetic-0001",
        "location": {"type": "S3", "s3Location": {"uri": s3_uri}},
        "metadata": metadata,
        "score": score,
    }


class _AgentRuntimeStub:
    """Stub bedrock-agent-runtime client capturing calls and returning a fixture."""

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
    """Install ``stub`` as the bedrock-agent-runtime client for any code path.

    Patches both ``boto3.client`` (used by strands_tools.retrieve) and, if
    present, the post-fix client builder in ``utils.kb_tools``.
    """
    # Local modules
    import utils.kb_tools as kb_tools

    real_boto3_client = __import__("boto3").client

    def fake_client(service_name: str, *args: Any, **kwargs: Any):
        if service_name == "bedrock-agent-runtime":
            return stub
        return real_boto3_client(service_name, *args, **kwargs)

    patches = [patch("boto3.client", side_effect=fake_client)]
    if hasattr(kb_tools, "_build_agent_runtime_client"):
        patches.append(patch("utils.kb_tools._build_agent_runtime_client", return_value=stub))
    # strands_tools.retrieve imports boto3 as a module attribute
    try:
        # Third-party packages
        import strands_tools.retrieve as st_retrieve  # noqa: F401

        patches.append(patch("strands_tools.retrieve.boto3.client", side_effect=fake_client))
    except Exception:
        pass

    started = [p.start() for p in patches]
    try:
        yield stub
    finally:
        for p in patches:
            p.stop()


def _call_tool(tool, **kwargs) -> str:
    """Invoke the undecorated tool function and coerce to the model-visible str."""
    fn = getattr(tool, "__wrapped__", None) or getattr(tool, "_tool_func")
    return str(fn(**kwargs))


# ---------------------------------------------------------------------------
# 1. Unbounded chunk text must not reach the model boundary verbatim.
# ---------------------------------------------------------------------------
def test_chunk_text_is_bounded_at_model_boundary():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool, get_projection_limits

    clear_kb_cache()
    limits = get_projection_limits()
    huge = "A" * 100_000  # 100 KB single chunk — far above any sane per-chunk cap
    stub = _AgentRuntimeStub({"retrievalResults": [_raw_result(text=huge)]})

    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        out = _call_tool(tool, text="scaling", numberOfResults=1, score=0.5)

    assert huge not in out
    assert len(out.encode("utf-8")) <= limits["max_envelope_bytes"]


# ---------------------------------------------------------------------------
# 2. Sensitive metadata / source location must be allowlisted, not dumped raw.
# ---------------------------------------------------------------------------
def test_sensitive_metadata_and_location_are_not_leaked():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    stub = _AgentRuntimeStub(
        {
            "retrievalResults": [
                _raw_result(
                    text="Use target-based scaling.",
                    s3_uri="s3://synthetic-internal-bucket/prod/deployment-9f3c/secret-runbook.md",
                    extra_metadata={
                        "internal-owner-alias": "synthetic-secret-team",
                        "deployment-id": "deploy-synthetic-42",
                    },
                )
            ]
        }
    )

    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        out = _call_tool(tool, text="scaling", numberOfResults=1, score=0.5)

    assert "deploy-synthetic-42" not in out
    assert "deployment-9f3c" not in out
    assert "secret-runbook" not in out
    assert "ds-synthetic-0001" not in out
    assert "internal-owner-alias" not in out
    assert "synthetic-secret-team" not in out


# ---------------------------------------------------------------------------
# 3. A continuation token (partial/truncated provider response) must NOT be
#    presented as a complete/empty result.
# ---------------------------------------------------------------------------
def test_continuation_token_is_not_reported_as_complete():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    stub = _AgentRuntimeStub(
        {
            "retrievalResults": [_raw_result(text="partial answer")],
            "nextToken": "synthetic-continuation-token",
        }
    )

    tool = create_kb_retrieve_tool("kb-synth-eks")
    with _provider(stub):
        out = _call_tool(tool, text="pods", numberOfResults=1, score=0.5)

    envelope = json.loads(out)
    assert envelope["state"] not in {"empty", "complete"}
    assert envelope["state"] in {"partial", "truncated"}
    assert "synthetic-continuation-token" not in out


# ---------------------------------------------------------------------------
# 4. Provider exception text must never reach the model boundary.
# ---------------------------------------------------------------------------
def test_provider_exception_text_is_not_model_visible():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    secret_detail = "AccessDeniedException: arn:aws:bedrock:us-west-2:secret detail leak"
    stub = _AgentRuntimeStub(exc=RuntimeError(secret_detail))

    tool = create_kb_retrieve_tool("kb-synth-cost")
    with _provider(stub):
        out = _call_tool(tool, text="spend", numberOfResults=1, score=0.5)

    assert secret_detail not in out
    assert "AccessDeniedException" not in out
    assert "arn:aws:bedrock" not in out
    envelope = json.loads(out)
    assert envelope["state"] in {"unavailable", "denied"}


# ---------------------------------------------------------------------------
# 5. A present, valid, empty collection stays "empty" (distinct from partial).
# ---------------------------------------------------------------------------
def test_valid_empty_collection_reports_empty():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    stub = _AgentRuntimeStub({"retrievalResults": []})

    tool = create_kb_retrieve_tool("kb-synth-cost")
    with _provider(stub):
        out = _call_tool(tool, text="spend", numberOfResults=3, score=0.5)

    envelope = json.loads(out)
    assert envelope["state"] == "empty"


# ---------------------------------------------------------------------------
# 6. numberOfResults must be clamped to the validated hard maximum.
# ---------------------------------------------------------------------------
def test_number_of_results_is_clamped_to_hard_max():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool, get_projection_limits

    clear_kb_cache()
    limits = get_projection_limits()
    stub = _AgentRuntimeStub({"retrievalResults": []})

    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        _call_tool(tool, text="scaling", numberOfResults=10_000, score=0.5)

    assert stub.calls, "provider retrieve was not called"
    requested = stub.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"]
    assert requested <= limits["max_results"]


# ---------------------------------------------------------------------------
# 7. Malformed provider response (missing retrievalResults) -> "malformed",
#    never "complete"/"empty".
# ---------------------------------------------------------------------------
def test_missing_retrieval_results_is_malformed():
    # Local modules
    from utils.kb_projection import project_retrieve_response
    from utils.kb_tools import EffectiveRequest

    eff = EffectiveRequest(kb_id="kb-synth", region="us-west-2", query="q", number_of_results=3, min_score=0.5)
    out = project_retrieve_response({"guardrailAction": "NONE"}, eff)
    envelope = json.loads(out)
    assert envelope["state"] == "malformed"
    assert envelope["errorCode"] == "KB_MALFORMED_RESPONSE"


def test_non_dict_provider_response_is_malformed():
    # Local modules
    from utils.kb_projection import project_retrieve_response
    from utils.kb_tools import EffectiveRequest

    eff = EffectiveRequest(kb_id="kb-synth", region="us-west-2", query="q", number_of_results=3, min_score=0.5)
    out = project_retrieve_response("not-a-dict", eff)
    assert json.loads(out)["state"] == "malformed"


# ---------------------------------------------------------------------------
# 8. Denied vs generic-unavailable is decided by the STRUCTURED error code, not
#    the free-form provider message.
# ---------------------------------------------------------------------------
def test_denied_classification_from_error_code_only():
    # Local modules
    from utils.kb_projection import KBErrorCode, classify_provider_error

    class _Denied(Exception):
        pass

    exc = _Denied("opaque")
    exc.response = {"Error": {"Code": "AccessDeniedException", "Message": "secret arn detail"}}
    assert classify_provider_error(exc) == KBErrorCode.DENIED

    generic = _Denied("boom")
    generic.response = {"Error": {"Code": "ThrottlingException"}}
    assert classify_provider_error(generic) == KBErrorCode.UNAVAILABLE


# ---------------------------------------------------------------------------
# 9. Deterministic truncation: same input -> byte-identical envelope.
# ---------------------------------------------------------------------------
def test_truncation_is_deterministic():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    raw = {"retrievalResults": [_raw_result(text="B" * 50_000)]}

    outs = []
    for _ in range(2):
        clear_kb_cache()
        stub = _AgentRuntimeStub(raw)
        tool = create_kb_retrieve_tool("kb-synth-eks")
        with _provider(stub):
            outs.append(_call_tool(tool, text="pods", numberOfResults=1, score=0.5))
    assert outs[0] == outs[1]
    env = json.loads(outs[0])
    assert env["results"][0]["textTruncated"] is True
    # A truncated chunk means the result is not a clean "complete".
    assert env["state"] == "truncated"


# ---------------------------------------------------------------------------
# 10. Cache identity includes the validated result count: different counts do
#     NOT collide.
# ---------------------------------------------------------------------------
def test_cache_key_includes_result_count():
    # Local modules
    from utils.kb_tools import EffectiveRequest, _get_cache_key

    a = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=3, min_score=0.5)
    b = EffectiveRequest(kb_id="kb", region="us-west-2", query="q", number_of_results=5, min_score=0.5)
    assert _get_cache_key(a) != _get_cache_key(b)


# ---------------------------------------------------------------------------
# 11. A dropped unusable row (present but non-text) must not look complete.
# ---------------------------------------------------------------------------
def test_dropped_unusable_row_is_not_complete():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    raw = {
        "retrievalResults": [
            _raw_result(text="usable chunk"),
            {  # present but unusable (no text content)
                "content": {"type": "IMAGE", "byteContent": "synthetic-bytes"},
                "location": {"type": "S3", "s3Location": {"uri": "s3://b/x.png"}},
                "score": 0.9,
            },
        ]
    }
    stub = _AgentRuntimeStub(raw)
    tool = create_kb_retrieve_tool("kb-synth-gamelift")
    with _provider(stub):
        out = _call_tool(tool, text="scaling", numberOfResults=5, score=0.5)
    env = json.loads(out)
    assert env["state"] == "truncated"
    assert env["returnedCount"] == 1
    assert "synthetic-bytes" not in out


# ---------------------------------------------------------------------------
# 12. Only projected envelopes are cached — a second identical call returns the
#     same safe envelope (never a raw payload).
# ---------------------------------------------------------------------------
def test_cache_returns_projected_envelope():
    # Local modules
    from utils.kb_tools import clear_kb_cache, create_kb_retrieve_tool

    clear_kb_cache()
    stub = _AgentRuntimeStub({"retrievalResults": [_raw_result(text="cached answer")]})
    tool = create_kb_retrieve_tool("kb-synth-cost")
    with _provider(stub):
        first = _call_tool(tool, text="spend", numberOfResults=1, score=0.5)
        second = _call_tool(tool, text="spend", numberOfResults=1, score=0.5)
    assert first == second
    # Provider called once; second served from cache.
    assert len(stub.calls) == 1
    json.loads(first)  # is valid JSON envelope, not a raw dict repr
