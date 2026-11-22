from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any

from ...core import ChatMessage, Macros, TurnCast, extract_hyperparams
from ...core.settings import Settings
from ...inference import LLMClient
from ...prompting import build_prefix

DEFAULT_SUMMARY_INSTRUCTIONS = (
    "[OOC: Write a rich prose narrative summary of the story so far. "
    "Preserve significant dialogue verbatim in quotes. "
    "Record key story beats, milestones, and relationship developments. "
    "Be thorough — this will be the sole context for the story's continuation.]"
)

REVISION_INSTRUCTIONS = "[OOC: Revise the summary above: {request}\nWrite out the whole revised summary, nothing else.]"


class ConversationSummarizer:
    def __init__(self, client: LLMClient, settings: Settings):
        self.client = client
        self.settings = settings

    def build_messages(
        self,
        system_prompt: str,
        char_persona: str,
        char_scenario: str,
        mes_example: str,
        post_history_instructions: str,
        history_slice: Sequence[Mapping[str, Any]],
        macros: Macros,
        user_description: str,
        custom_instructions: str | None = None,
        *,
        cast: TurnCast | None = None,
        speaker_names: Mapping[str, str] | None = None,
        revision: tuple[str, str] | None = None,
    ) -> list[ChatMessage]:
        """The summary request over *history_slice*.

        *revision* is ``(draft, request)``: the draft is replayed as the model's own answer and the request asks for a rewrite of
        it, so a revision shares the whole history prefix with the summary it revises.
        """
        prefix = build_prefix(
            system_prompt,
            char_persona,
            char_scenario,
            mes_example,
            post_history_instructions,
            history_slice,
            macros,
            user_description,
            cast=cast,
            speaker_names=speaker_names,
        )
        instructions = DEFAULT_SUMMARY_INSTRUCTIONS
        if custom_instructions:
            instructions += f"\n{custom_instructions}"
        messages: list[ChatMessage] = [*prefix, {"role": "user", "content": instructions}]
        if revision:
            draft, request = revision
            messages.append({"role": "assistant", "content": draft})
            messages.append({"role": "user", "content": REVISION_INSTRUCTIONS.format(request=request)})
        return messages

    async def stream(self, llm_messages: Sequence[Mapping[str, Any]], model: str) -> AsyncGenerator[str, None]:
        # Writer lane: the summary is prose in the user's own preset, and it is written with the model that writes the story.
        params = extract_hyperparams(self.settings)
        async for chunk in self.client.complete(llm_messages, model, **params):
            if chunk["type"] == "content":
                yield chunk["delta"]
