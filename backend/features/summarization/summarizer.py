from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from ...core import ChatMessage, extract_hyperparams
from ...core.settings import Settings
from ...inference import CachedBase, KVCacheTracker, LLMClient, reasoning_cfg
from ...prompting.layout import TOOL_NOTICE

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


def summary_request(
    custom_instructions: str | None = None, *, revision: tuple[str, str] | None = None, tool_notice: bool = False
) -> list[ChatMessage]:
    """The messages after the history the summary is written from.

    *revision* is ``(draft, request)``: the draft is replayed as the model's own answer and the request asks for a rewrite of
    it, so a revision shares the whole history prefix with the summary it revises. *tool_notice* leads the request when the call
    carries the turn's tool schemas, as it does the Writer's.
    """
    custom = custom_instructions.strip() if custom_instructions else ""
    instructions = SUMMARY_INSTRUCTIONS.format(custom=f"\n{custom}" if custom else "")
    if tool_notice:
        instructions = f"{TOOL_NOTICE}\n\n{instructions}"
    messages: list[ChatMessage] = [{"role": "user", "content": instructions}]
    if revision:
        draft, request = revision
        messages.append({"role": "assistant", "content": draft})
        messages.append({"role": "user", "content": REVISION_INSTRUCTIONS.format(request=request)})
    return messages


async def stream_summary(
    client: LLMClient,
    base: CachedBase,
    settings: Settings,
    request: Sequence[ChatMessage],
    *,
    reasoning_on: bool,
    reasoning_prefill: str = "",
    kv_tracker: KVCacheTracker | None = None,
) -> AsyncIterator[str]:
    """Stream the summary as a Writer call extending *base*, the turn's Writer base over the summarized history, so the call
    reads that turn's cached prefix and only *request* is new."""
    async for item in base.complete(
        client,
        label="summary",
        trailing=request,
        tool_choice="none" if base.tools else None,
        kv_tracker=kv_tracker,
        **reasoning_cfg(reasoning_on, reasoning_prefill),
        **extract_hyperparams(settings),
    ):
        if item["type"] == "done":
            return
        if item["type"] == "content":
            yield item["delta"]
