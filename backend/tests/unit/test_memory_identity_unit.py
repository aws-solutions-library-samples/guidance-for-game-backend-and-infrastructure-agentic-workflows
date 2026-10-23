"""Unit tests for the pseudonymous AgentCore Memory actor ID.

AgentCore Memory receives the actor ID on every event call and uses it as the
long-term namespace. When tracing is enabled on the memory resource, the memory
service emits trace spans that carry the actor ID as a span attribute, so passing
the raw Cognito subject there puts a stable user identifier into exported
telemetry. The runtime therefore hands the memory service an opaque, deterministic
digest of the verified subject instead.
"""

# Standard library
import hashlib
import re
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
import agents.orchestrator as orch
from utils.memory_identity import memory_actor_id

# Capture the REAL run_orchestrator at import time; conftest's autouse fixture
# patches the module attribute for the duration of each test.
_real_run_orchestrator = orch.run_orchestrator

pytestmark = pytest.mark.unit

# Synthetic Cognito-style subject; never a real identifier.
SUBJECT = "3f2a9c1e-7b4d-4e8a-9c6f-1a2b3c4d5e6f"
OTHER_SUBJECT = "9d8c7b6a-5f4e-4d3c-8b2a-0f1e2d3c4b5a"


def test_memory_actor_id_is_opaque_hex_and_omits_the_subject():
    actor = memory_actor_id(SUBJECT)

    assert re.fullmatch(r"[0-9a-f]{64}", actor)
    assert SUBJECT.replace("-", "") not in actor


def test_memory_actor_id_is_stable_and_distinct_per_subject():
    assert memory_actor_id(SUBJECT) == memory_actor_id(SUBJECT)
    assert memory_actor_id(SUBJECT) != memory_actor_id(OTHER_SUBJECT)


def test_memory_actor_id_matches_pinned_known_answers():
    # Changing the domain string, the hash, or how the subject is normalized gives
    # every user a new actor ID and orphans the memory stored under the old one, so
    # the value itself is part of the contract. Each vector was computed outside the
    # code under test:
    #   printf '%s' 'gbaw:agentcore-memory-actor:v1:<subject>' | shasum -a 256
    assert memory_actor_id(SUBJECT) == "d6210547095ae60cfd77b11fc474fe5fcdc023653d0ee7c98dfedf9767e630fe"
    # Case is part of the subject; nothing is lower-cased.
    assert memory_actor_id(SUBJECT.upper()) == "79c86c31653a5b6a1e6f58dd4d8a35d9cc36cde41619ecb719e7553118c9fa70"


def test_memory_actor_id_hashes_non_ascii_subjects_as_utf8_without_normalization():
    # Fullwidth "U", a decomposed "e" plus combining acute accent, and CJK text.
    # Unicode normalization (NFC or NFKC) would change this subject, and a different
    # encoding would change its bytes; the digest below was computed outside the
    # code under test from the UTF-8 bytes of the prefix and the subject.
    subject = "\uff35ser-e\u0301-\u65e5\u672c"

    assert memory_actor_id(subject) == "9bcc450b73647b42998b340bfd3d6618d1432fdeb65ed53761dd8de6bd3b0822"


def test_memory_actor_id_does_not_trim_the_subject():
    assert memory_actor_id(f" {SUBJECT}") != memory_actor_id(SUBJECT)
    assert memory_actor_id(f"{SUBJECT}\n") != memory_actor_id(SUBJECT)


def test_memory_actor_id_is_domain_separated_from_a_plain_digest():
    # A bare SHA-256 of the subject would equal the digest any other component
    # might compute for the same value; the memory actor ID must not.
    assert memory_actor_id(SUBJECT) != hashlib.sha256(SUBJECT.encode("utf-8")).hexdigest()


def test_memory_actor_id_fits_the_agentcore_actor_id_contract():
    # Read the constraints from the installed service model, which is what the
    # service enforces, so a model update moves this test with it. The model is
    # loaded from the package rather than through a botocore session, which would
    # also resolve the AWS profile named in the environment.
    # Third-party packages
    from botocore.loaders import Loader
    from botocore.model import ServiceModel

    model = Loader().load_service_model("bedrock-agentcore", "service-2")
    shape = ServiceModel(model, service_name="bedrock-agentcore").shape_for("ActorId")
    actor = memory_actor_id(SUBJECT)

    assert shape.metadata["min"] <= len(actor) <= shape.metadata["max"]
    assert re.fullmatch(shape.metadata["pattern"], actor)


# Not usable as a subject: wrong type, empty, whitespace-only (including Unicode
# whitespace such as a no-break space), or text that cannot be encoded (a lone
# surrogate). A whitespace-only subject would hash to a value shared by every caller
# that sends one.
UNUSABLE_SUBJECTS = ["", "   ", "\n\t", "\u00a0", None, 123, True, b"bytes", ["x"], {"a": 1}, "\ud800abc"]


@pytest.mark.parametrize("bad", UNUSABLE_SUBJECTS)
def test_memory_actor_id_rejects_anything_but_a_usable_string(bad):
    with pytest.raises(ValueError):
        memory_actor_id(bad)


@pytest.mark.parametrize("bad", ["\ud800-synthetic-marker", b"synthetic-marker", ["synthetic-marker"]])
def test_memory_actor_id_error_does_not_echo_the_subject(bad):
    # The error text and its chained cause end up in logs; neither may carry the
    # raw value the caller tried to use.
    with pytest.raises(ValueError) as excinfo:
        memory_actor_id(bad)

    assert "synthetic-marker" not in str(excinfo.value)
    # A codec error keeps the whole text it failed on, so nothing may be chained.
    assert "synthetic-marker" not in repr(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def _run_orchestrator(context, *, semantic_available=True):
    """Run the real orchestrator with memory enabled and every provider stubbed."""
    captured = {"config_calls": [], "session_manager_calls": [], "saves": [], "agent_calls": []}

    def fake_config(**kwargs):
        captured["config_calls"].append(kwargs)
        return MagicMock()

    def fake_session_manager(**kwargs):
        captured["session_manager_calls"].append(kwargs)
        return MagicMock()

    def fake_save(*args, **kwargs):
        captured["saves"].append(args)

    with (
        patch.object(orch, "USE_BEDROCK_SESSIONS", True),
        patch.object(orch, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-test-123"),
        patch.object(orch, "AgentCoreMemoryConfig", side_effect=fake_config),
        patch.object(orch, "AgentCoreMemorySessionManager", side_effect=fake_session_manager),
        patch.object(orch, "create_bedrock_model_with_overrides", MagicMock()),
        patch.object(orch, "create_cached_bedrock_model", MagicMock()),
        patch.object(orch, "SEMANTIC_MEMORY_AVAILABLE", semantic_available),
        patch.object(orch, "extract_and_save_user_info", side_effect=fake_save, create=True),
        patch.object(orch, "Agent") as mock_agent,
    ):
        mock_agent.return_value.return_value = "ok"
        _real_run_orchestrator("hello", context=context)
        captured["agent_calls"] = list(mock_agent.call_args_list)
    return captured


def test_orchestrator_gives_memory_the_pseudonymous_actor_id():
    captured = _run_orchestrator({"user_id": SUBJECT, "session_id": "s-1"})

    assert len(captured["config_calls"]) == 1
    config = captured["config_calls"][0]
    assert config["actor_id"] == memory_actor_id(SUBJECT)
    assert config["session_id"] == "s-1"
    # The long-term namespace template still resolves from the actor ID.
    assert list(config["retrieval_config"]) == ["{actorId}"]


def test_orchestrator_never_hands_the_raw_subject_to_memory_or_the_agent():
    # The agent constructor is covered too: trace attributes and similar keyword
    # arguments end up on exported spans, which is the exposure this change closes.
    captured = _run_orchestrator({"user_id": SUBJECT, "session_id": "s-1"})

    assert captured["agent_calls"], "the agent should have been constructed"
    everything = "".join(
        repr(captured[key]) for key in ("config_calls", "session_manager_calls", "saves", "agent_calls")
    )
    assert SUBJECT not in everything


def test_orchestrator_saves_semantic_memory_under_the_same_pseudonymous_actor_id():
    captured = _run_orchestrator({"user_id": SUBJECT, "session_id": "s-1"})

    assert captured["saves"], "semantic memory extraction should run when memory is enabled"
    assert captured["saves"][0][0] == memory_actor_id(SUBJECT)


def test_orchestrator_accepts_the_actor_id_context_field():
    captured = _run_orchestrator({"actor_id": SUBJECT, "session_id": "s-1"})

    assert captured["config_calls"][0]["actor_id"] == memory_actor_id(SUBJECT)


@pytest.mark.parametrize("identity", UNUSABLE_SUBJECTS)
def test_orchestrator_disables_memory_when_the_identity_is_not_a_usable_string(identity):
    captured = _run_orchestrator({"user_id": identity, "session_id": "s-1"})

    assert captured["config_calls"] == []
    assert captured["session_manager_calls"] == []
    assert captured["saves"] == []
