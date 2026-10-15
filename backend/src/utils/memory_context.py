"""Mark session-manager-injected memory so it bypasses the input Guardrail.

The input Guardrail is attached with ``guardrail_latest_message=True``. Strands
wraps every ``text``/``image`` block of the last user text message in a
``guardContent`` block, and Bedrock Converse then evaluates only the guarded
blocks as input. The AgentCore session manager injects retrieved long-term
memory as an extra ``text`` block in that same message, which would otherwise be
evaluated as if the user had just typed it.

:class:`MemoryScopedSessionManager` tags the exact block it inserts with
:data:`MEMORY_CONTEXT_MARKER_KEY`, a private marker key that user input cannot
forge. :class:`~models.guardrail_scope.GuardrailScopedBedrockModel` sends the
marked block as plain ``text`` while still wrapping the user's own blocks. The
marker is a private dict key, and Strands strips it from the Converse request.
"""

# Third-party packages
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands.hooks import MessageAddedEvent

# Local modules
from models.guardrail_scope import MEMORY_CONTEXT_MARKER_KEY

__all__ = ["MEMORY_CONTEXT_MARKER_KEY", "MemoryScopedSessionManager"]


class MemoryScopedSessionManager(AgentCoreMemorySessionManager):
    """Session manager that marks the memory block it injects.

    Behaves exactly like :class:`AgentCoreMemorySessionManager` except that the
    single context block it adds to the latest user message is tagged with
    :data:`MEMORY_CONTEXT_MARKER_KEY`, so the Guardrail-scoped model can send it
    unwrapped. Marking fails closed: if the base class adds anything other than
    one recognizable context block, nothing is marked and the block stays
    input-checked.
    """

    def retrieve_customer_context(self, event: MessageAddedEvent) -> None:
        """Inject retrieved memory, de-duplicate its lines, then mark it.

        The block is identified by object identity (a block present after
        ``super()`` that was not present before) and verified by shape: exactly
        one block was added, it is a dict whose ``text`` is a ``str``, and that
        text is wrapped in the configured context tag. A block that passes those
        checks has its lines de-duplicated (normalized comparison, first
        occurrence kept, order preserved) and is then marked. Any other outcome
        marks nothing and leaves the content wrapped by the Guardrail.
        """
        messages = getattr(event.agent, "messages", None)
        content = messages[-1].get("content") if messages else None
        before_ids = {id(block) for block in content} if content else set()

        super().retrieve_customer_context(event)

        if not content:
            return
        added = [block for block in content if id(block) not in before_ids]
        if len(added) != 1:
            return
        block = added[0]
        if not isinstance(block, dict):
            return
        text = block.get("text")
        tag = self.config.context_tag
        if isinstance(text, str) and text.startswith(f"<{tag}>") and text.endswith(f"</{tag}>"):
            block["text"] = _dedupe_context_block(text, tag)
            block[MEMORY_CONTEXT_MARKER_KEY] = True


def _dedupe_context_block(text: str, tag: str) -> str:
    """Collapse normalized-equal lines inside a ``<tag>…</tag>`` block.

    Keeps the first occurrence of each line and preserves order. Comparison is
    case-insensitive and whitespace-collapsed; the original (non-normalized)
    first occurrence is the one retained. This assumes single-line records: the
    base class joins records with ``\\n``, and the only long-term writer stores
    single-line pattern facts, so a line maps to one record.
    """
    inner = text[len(f"<{tag}>") : -len(f"</{tag}>")]
    seen: set = set()
    kept = []
    for line in inner.split("\n"):
        key = " ".join(line.split()).lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return f"<{tag}>" + "\n".join(kept) + f"</{tag}>"
