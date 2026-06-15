from __future__ import annotations

from typing import Any, AsyncGenerator, Mapping, Sequence

from ...inference import prompt_builder
from ...inference import LLMClient
from ...core import ChatMessage, Macros

DEFAULT_SUMMARY_INSTRUCTIONS = (
    "[OOC: Write a rich prose narrative summary of the story so far. "
    "Preserve significant dialogue verbatim in quotes. "
    "Record key story beats, milestones, and relationship developments. "
    "Be thorough — this will be the sole context for the story's continuation.]"
)

_LLM_PARAMS = (
    "temperature",
    "max_tokens",
    "top_p",
    "min_p",
    "top_k",
    "repetition_penalty",
)


class ConversationSummarizer:
    def __init__(self, client: LLMClient, settings: Mapping[str, Any]):
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
    ) -> list[ChatMessage]:
        prefix = prompt_builder.build_prefix(
            system_prompt,
            char_persona,
            char_scenario,
            mes_example,
            post_history_instructions,
            history_slice,
            macros,
            user_description,
        )
        instructions = DEFAULT_SUMMARY_INSTRUCTIONS
        if custom_instructions:
            instructions += f"\n{custom_instructions}"
        return prefix + [{"role": "user", "content": instructions}]

    async def stream(self, llm_messages: Sequence[Mapping[str, Any]], model: str) -> AsyncGenerator[str, None]:
        params = {k: v for k in _LLM_PARAMS if (v := self.settings.get(k)) is not None}
        async for chunk in self.client.complete(llm_messages, model, **params):
            if chunk["type"] == "content":
                yield chunk["delta"]
