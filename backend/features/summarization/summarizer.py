from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any

from ...core import ChatMessage, Macros, TurnCast, extract_hyperparams
from ...core.settings import Settings
from ...inference import LLMClient
from ...prompting import build_prefix
from ...prompting.layout import SectionInputs

# No category list here ("beats, milestones, relationships"): models turn every named category into a heading. Format bans come
# last, ahead of only the user's own instructions, which may override them.
SUMMARY_INSTRUCTIONS = (
    "[OOC: Pause the roleplay. Retell the story so far as a narrator would, in plain prose paragraphs, in the order it "
    "happened. The story continues from this retelling alone, so keep who everyone is, what happened, and what is still "
    "unresolved, and stop at the latest moment of the story. Quote dialogue word for word only where the exact words matter later.\n"
    "No headings, lists, bold text, or labels. No title, preface, or closing note: start with the story itself.{custom}]"
)

REVISION_INSTRUCTIONS = (
    "[OOC: Revise the retelling above: {request}\nWrite out the whole revised retelling in the same plain prose, nothing else.]"
)


class ConversationSummarizer:
    def __init__(self, client: LLMClient, settings: Settings):
        self.client = client
        self.settings = settings

    def build_messages(
        self,
        system_prompt: str,
        sections: SectionInputs,
        history_slice: Sequence[Mapping[str, Any]],
        macros: Macros,
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
        prefix = build_prefix(system_prompt, sections, history_slice, macros, cast=cast, speaker_names=speaker_names)
        custom = custom_instructions.strip() if custom_instructions else ""
        instructions = SUMMARY_INSTRUCTIONS.format(custom=f"\n{custom}" if custom else "")
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
