"""Document-mode continuation policy — prompt shape + transport choice.

A user feature (not a pipeline pass), byte-symmetric with
``features/summarization``: it owns the fallback instruction, the chat-message
shape, the transport branch, and the delta filter; the route
(``api/routes/documents.py``) owns the HTTP. Depends only downward on
``inference`` + ``core``.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Mapping

from ...core import ChatMessage, extract_hyperparams
from ...inference import LLMClient, reasoning_cfg

# Single place to iterate on chat-fallback quality. Text mode (raw /completion)
# is the recommended path; this only fires on chat-completion endpoints, where
# assistant-continuation is unreliable so we frame it as a system instruction +
# the document prefix as the user turn.
DOC_CHAT_INSTRUCTION = (
    "You are a writing assistant that continues the user's text. "
    "Continue seamlessly from exactly where it stops, matching its voice, tense, and style. "
    "Output only the continuation — no preamble, no commentary, no quotation of the existing text."
)


class DocumentContinuer:
    def __init__(self, client: LLMClient, settings: Mapping[str, Any]):
        self.client = client
        # guard an unset max_tokens: a raw /completion with n_predict=-1 runs away.
        self.settings = settings
        self.params = extract_hyperparams(settings, defaults={"max_tokens": 512})

    def build_chat_messages(self, prompt: str) -> list[ChatMessage]:
        return [
            {"role": "system", "content": DOC_CHAT_INSTRUCTION},
            {"role": "user", "content": prompt},
        ]

    async def stream(self, prompt: str, model: str) -> AsyncGenerator[str, None]:
        # Transport branch on the client's own completion_mode (single source of
        # truth — not a second settings read):
        #   text -> raw /completion continuation (preferred, no chat template)
        #   else -> chat fallback with thinking suppressed
        if self.client.completion_mode == "text":
            gen = self.client.complete_raw(prompt, model, **self.params)
        else:
            gen = self.client.complete(self.build_chat_messages(prompt), model, **self.params, **reasoning_cfg(False))
        # Yield content deltas only (drop reasoning) — same filter as
        # ConversationSummarizer.stream.
        async for chunk in gen:
            if chunk["type"] == "content":
                yield chunk["delta"]
