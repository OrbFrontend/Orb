"""Document-mode continuation policy — prompt shape + transport choice.

A user feature (not a pipeline pass), byte-symmetric with
``features/summarization``: it owns the fallback instruction, the chat-message
shape, the transport branch, and the delta filter; the route
(``api/routes/documents.py``) owns the HTTP. Depends only downward on
``inference`` + ``core``.

Two prompting strategies, chosen per request by the ``assisted`` flag:

* **Raw** (default) — the document is sent verbatim (text transport) or wrapped
  as a single user turn (chat fallback); the user hand-types any chat-template
  control tokens. Mikupad-style; unchanged behaviour.
* **Assisted** — the document is a transcript written with readable line macros
  (``### SYSTEM:`` / ``### USER:`` / ``### ASSISTANT:``); :func:`parse_doc_macros`
  renders it to chat messages + an open prefill and lets the model's own
  template supply BOS/turn markers. See ``docs/features/document-mode.md``.
"""

from __future__ import annotations

import re
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

# Default ``### SYSTEM:`` fill for assisted mode (distinct from
# DOC_CHAT_INSTRUCTION, which frames the chat-endpoint fallback). Used whenever
# the document carries no ``### SYSTEM:`` line.
DOC_ASSIST_INSTRUCTION = (
    "You are a co-writer. Continue the document in the same voice, tense, and style, "
    "and follow any notes the author leaves. Output only the continuation — no preamble, no commentary."
)

# Chat-transport only: /chat/completions drops the open prefill, so on that path
# we close the prefill as an assistant turn and append this user turn to re-anchor
# the model on continuing the text. Text mode never uses this (it renders the
# prefill as a genuinely open assistant turn).
DOC_ASSIST_CONTINUE = "Continue the text exactly from where it stops — no preamble."

# Default user turn when the document carries no ``### USER:`` note (and as
# alternation filler before leading prose). The length hint is load-bearing:
# probe-verified on Qwen that a bare "Continue the text." stops after 6–9 tokens
# while the hint stretches it to 50–300.
_DEFAULT_USER = "Continue the text. Write several paragraphs."

# Line-anchored role macro: ``### ROLE: inline content``. Case-insensitive; a
# single optional space after the colon is the delimiter (further spaces are
# content). Only interpreted in assisted mode — in Raw mode these lines are
# literal prose.
_MACRO_RE = re.compile(r"^###\s*(SYSTEM|USER|ASSISTANT)\s*:\s?(.*)$", re.IGNORECASE)

# Per-token-alternatives counts, requested only when the client toggles probs on.
# Text mode (llama.cpp /completion) matches mikupad's default of 10; chat mode
# asks for 5, a safe floor across OpenAI-compat providers that support logprobs.
_N_PROBS_TEXT = 10
_TOP_LOGPROBS_CHAT = 5


def _msg(role: str, content: str) -> ChatMessage:
    """Build a ChatMessage, narrowing *role* to the TypedDict's Literal."""
    if role == "system":
        return {"role": "system", "content": content}
    if role == "user":
        return {"role": "user", "content": content}
    return {"role": "assistant", "content": content}


def parse_doc_macros(text: str) -> tuple[list[ChatMessage], str | None]:
    """Parse an assisted-mode document into chat messages + an open prefill.

    A flat line scan (no message tree): each line is a SYSTEM/USER/ASSISTANT
    macro or continuation prose. SYSTEM lines are hoisted into one front system
    turn; the remaining USER lines and prose coalesce into alternating
    user/assistant runs in document order.

    Returns ``(messages, prefill)``:

    * ``messages`` always starts ``[system, user]`` and strictly alternates, so
      appending the prefill as an open assistant turn preserves alternation.
    * ``prefill`` is the final prose block verbatim, or ``None`` when the
      document ends with a note (fresh-turn generation) or the final prose is
      whitespace-only.

    See ``docs/features/document-mode.md`` for the convention and rationale.
    """
    system_parts: list[str] = []
    # Alternating runs by construction: prose accumulates until a USER line and
    # vice versa. Each entry is [role, [line, ...]] with role in {user, assistant}.
    blocks: list[tuple[str, list[str]]] = []

    def _accumulate(role: str, line: str) -> None:
        if blocks and blocks[-1][0] == role:
            blocks[-1][1].append(line)
        else:
            blocks.append((role, [line]))

    for line in text.split("\n"):
        m = _MACRO_RE.match(line)
        if m:
            macro_role = m.group(1).lower()
            content = m.group(2)
            if not content.strip():
                continue  # empty macro content → ignored (line drops out)
            if macro_role == "system":
                system_parts.append(content)
            elif macro_role == "user":
                _accumulate("user", content)
            else:  # ### ASSISTANT: → inline content joins the surrounding prose
                _accumulate("assistant", content)
        else:
            _accumulate("assistant", line)  # non-macro line → continuation prose

    system_content = "\n".join(system_parts) if system_parts else DOC_ASSIST_INSTRUCTION

    # The final prose block is the open prefill; a whitespace-only prefill drops
    # to None (→ fresh-turn generation under the last note).
    prefill: str | None = None
    if blocks and blocks[-1][0] == "assistant":
        prefill = "\n".join(blocks.pop()[1])
        if not prefill.strip():
            prefill = None

    # Closed turns: right-trim assistant prose (cosmetic inside a closed turn)
    # and drop whitespace-only prose so notes on either side coalesce; merge any
    # now-adjacent user runs (dropping a prose block can leave two side by side).
    body: list[list[str]] = []  # [role, text]
    for role, lines in blocks:
        block_text = "\n".join(lines)
        if role == "assistant":
            block_text = block_text.rstrip()
            if not block_text:
                continue
        if body and body[-1][0] == role:
            body[-1][1] = body[-1][1] + "\n" + block_text
        else:
            body.append([role, block_text])

    # Messages must open [system, user] and alternate. Insert the default user
    # turn when the body opens with prose (leading-prose filler) or is empty
    # (macro-free/no-USER docs → the validated 3-turn shape with whole-doc prefill).
    if not body or body[0][0] == "assistant":
        body.insert(0, ["user", _DEFAULT_USER])

    messages: list[ChatMessage] = [_msg("system", system_content)]
    for role, block_text in body:
        messages.append(_msg(role, block_text))
    return messages, prefill


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

    async def stream(
        self, prompt: str, model: str, assisted: bool = False, token_probs: bool = False
    ) -> AsyncGenerator[dict, None]:
        # Transport branch on the client's own completion_mode (single source of
        # truth — not a second settings read), crossed with the assisted flag:
        #
        #   text  + raw       -> raw /completion continuation (preferred; verbatim)
        #   text  + assisted  -> parsed multi-turn + open prefill (F9 open-turn path)
        #   chat  + raw       -> chat fallback with thinking suppressed
        #   chat  + assisted  -> parsed multi-turn; prefill closed + re-anchor turn
        #                        (chat transport drops the open prefill)
        #
        # Reasoning is always off in assisted mode: a no-op on the text/prefill
        # path (client drops chat_template_kwargs there) but load-bearing for the
        # chat fallback and the trailing-note generation prompt.
        #
        # token_probs adds the per-transport alternatives request (mikupad-style
        # token swapping): n_probs on the llama.cpp branches, logprobs/top_logprobs
        # on the OpenAI-compat branches. Unset → no extra fields, unchanged bodies.
        probs_text = {"n_probs": _N_PROBS_TEXT} if token_probs else {}
        probs_chat = {"logprobs": True, "top_logprobs": _TOP_LOGPROBS_CHAT} if token_probs else {}
        if self.client.completion_mode == "text":
            if assisted:
                messages, prefill = parse_doc_macros(prompt)
                gen = self.client.complete(
                    messages, model, prefill=prefill, **self.params, **probs_text, **reasoning_cfg(False)
                )
            else:
                gen = self.client.complete_raw(prompt, model, **self.params, **probs_text)
        else:
            if assisted:
                messages, prefill = parse_doc_macros(prompt)
                if prefill:
                    # Close the prefill and re-anchor: /chat/completions cannot leave
                    # a trailing assistant turn open, so respond-style is the only
                    # reliable framing here (quality is model-dependent — text mode
                    # is the recommended assisted path).
                    messages = [
                        *messages,
                        _msg("assistant", prefill),
                        _msg("user", DOC_ASSIST_CONTINUE),
                    ]
                gen = self.client.complete(messages, model, **self.params, **probs_chat, **reasoning_cfg(False))
            else:
                gen = self.client.complete(
                    self.build_chat_messages(prompt), model, **self.params, **probs_chat, **reasoning_cfg(False)
                )
        # Yield content + token_probs chunks (drop reasoning). The route frames
        # content as `event: token` and token_probs as `event: probs`.
        async for chunk in gen:
            if chunk["type"] in ("content", "token_probs"):
                yield chunk
