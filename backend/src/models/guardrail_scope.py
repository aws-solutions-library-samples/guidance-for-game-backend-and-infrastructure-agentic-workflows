"""Keep marked content blocks out of the input Guardrail's scope.

The input Guardrail is attached with ``guardrail_latest_message=True``. Strands
wraps every ``text``/``image`` block of the last user text message in a
``guardContent`` block, and Bedrock Converse then evaluates only the guarded
blocks as input. When the AgentCore session manager injects retrieved long-term
memory as an extra ``text`` block in that same message, that block would
otherwise be evaluated as if the user had just typed it.

:class:`GuardrailScopedBedrockModel` sends any block carrying
:data:`MEMORY_CONTEXT_MARKER_KEY` as plain ``text`` while still wrapping the
user's own ``text``/``image`` blocks in ``guardContent``. The marker is a
private dict key; Strands' ``_format_request_message_content`` keeps only
Bedrock-known fields, so Strands strips the key from the Converse request.

This module intentionally does not import the AgentCore session manager, so the
model layer stays independent of the memory integration.
"""

# Standard library
import secrets
from typing import Any, cast

# Third-party packages
from strands.models import BedrockModel
from strands.types.content import Messages

# Private key used to tag a session-manager-injected memory block. User message
# content cannot set this key, and Strands strips it before the Converse call.
MEMORY_CONTEXT_MARKER_KEY = "_gbaw_memory_context"


class GuardrailScopedBedrockModel(BedrockModel):
    """Bedrock model that excludes marked blocks from input Guardrail scope.

    When ``guardrail_latest_message`` is set, the base class wraps every
    ``text``/``image`` block of the last user text message in ``guardContent``.
    This override lets the base class run unchanged, then rewrites each block
    marked with :data:`MEMORY_CONTEXT_MARKER_KEY` back to a plain ``text`` block,
    so only the user's own words are evaluated as input. A per-call random nonce
    identifies the marked blocks after wrapping, so block text content and
    position are never used to recognize them.
    """

    def _format_bedrock_messages(self, messages: Messages) -> list[dict[str, Any]]:
        if not self.config.get("guardrail_latest_message", False):
            return super()._format_bedrock_messages(messages)

        last_user_idx = self._find_last_user_text_message_index(messages)
        if last_user_idx is None:
            return super()._format_bedrock_messages(messages)

        # Map a fresh nonce to each marked block's original text, without
        # mutating the caller's message list. Only the last user text message
        # needs rewriting because that is the only message the base class wraps.
        nonce_to_text: dict[str, str] = {}
        staged_messages: list[Any] = list(messages)
        source: dict[str, Any] = dict(staged_messages[last_user_idx])
        staged_content: list[Any] = []
        for block in source.get("content", []):
            block_dict: dict[str, Any] = dict(block)
            if block_dict.get(MEMORY_CONTEXT_MARKER_KEY) and "text" in block_dict:
                nonce = secrets.token_hex(16)
                nonce_to_text[nonce] = block_dict["text"]
                staged_block = {key: value for key, value in block_dict.items() if key != MEMORY_CONTEXT_MARKER_KEY}
                staged_block["text"] = nonce
                staged_content.append(staged_block)
            else:
                staged_content.append(block)
        source["content"] = staged_content
        staged_messages[last_user_idx] = source

        formatted = super()._format_bedrock_messages(cast(Messages, staged_messages))
        if not nonce_to_text:
            return formatted

        for message in formatted:
            new_content: list[dict[str, Any]] = []
            for block in message.get("content", []):
                nonce = block.get("guardContent", {}).get("text", {}).get("text")
                if nonce in nonce_to_text:
                    new_content.append({"text": nonce_to_text[nonce]})
                else:
                    new_content.append(block)
            message["content"] = new_content
        return formatted
