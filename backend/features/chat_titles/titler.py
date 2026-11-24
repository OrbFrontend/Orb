"""Ask the Agent lane for a chat title over the chat's own cached prefix."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from ...core import ChatMessage, extract_hyperparams
from ...core.settings import Settings
from ...inference import CachedBase, KVCacheTracker, LLMClient, reasoning_cfg
from ...prompting.layout import TOOL_NOTICE

TITLE_INSTRUCTIONS = (
    "[OOC: Pause the roleplay. Give this story a title: two to five words that capture what it is about, in title case, "
    "like a chapter heading. Not a character's name on its own. Reply with the title only.]"
)
RETRY_INSTRUCTIONS = "[OOC: {reason} Reply with a different title only: two to five words.]"

_MAX_WORDS = 7
_MAX_CHARS = 60
_WRAPPERS = "\"'“”‘’*_`#"
_LABEL = re.compile(r"^title\s*:\s*", re.IGNORECASE)
_SUFFIXES = re.compile(r"(?: · Part \d+| \(checkpoint\)| \(continued\))+$")


def title_request(*, tool_notice: bool) -> list[ChatMessage]:
    """The one message after the history that asks for the title; *tool_notice* leads it when the call carries tool schemas."""
    text = f"{TOOL_NOTICE}\n\n{TITLE_INSTRUCTIONS}" if tool_notice else TITLE_INSTRUCTIONS
    return [{"role": "user", "content": text}]


def _title_key(title: str) -> str:
    return " ".join(_SUFFIXES.sub("", title.strip()).split()).casefold()


def _clean(raw: str) -> str:
    title = _LABEL.sub("", raw.strip()).strip(_WRAPPERS).strip()
    return _LABEL.sub("", title).strip(_WRAPPERS).strip()


def _rejection(raw: str, title: str, taken: set[str]) -> str | None:
    """Why *title* (cleaned from *raw*) cannot name the chat, as the sentence the corrective request opens with."""
    lines = [line for line in raw.strip().splitlines() if line.strip()]
    if (
        len(lines) != 1
        or not title
        or len(title.split()) > _MAX_WORDS
        or len(title) > _MAX_CHARS
        or (title.endswith(".") and not title.endswith(".."))
    ):
        return "That reply is not a title."
    if _title_key(title) in taken:
        return f"Another chat is already called “{title}”."
    return None


async def _ask(
    client: LLMClient,
    base: CachedBase,
    settings: Settings,
    request: Sequence[ChatMessage],
    *,
    reasoning: dict,
    kv_tracker: KVCacheTracker | None,
) -> str:
    text = ""
    async for item in base.complete(
        client,
        label="chat_title",
        trailing=request,
        tool_choice="none" if base.tools else None,
        kv_tracker=kv_tracker,
        **reasoning,
        **extract_hyperparams(settings, lane="agent"),
    ):
        if item["type"] == "content":
            text += item["delta"]
        elif item["type"] == "done":
            return text or str(item["message"].get("content") or "")
    return text


async def name_chat(
    client: LLMClient,
    base: CachedBase,
    settings: Settings,
    request: Sequence[ChatMessage],
    *,
    taken: Iterable[str],
    reasoning_on: bool,
    reasoning_prefill: str = "",
    kv_tracker: KVCacheTracker | None = None,
) -> str | None:
    """A title for the chat *base* holds, or None when the model gives none it can use.

    The call extends *base*, the next turn's prefix over the chat's history, by *request*. A reply that is not a title, or one
    another chat in *taken* already carries, is replayed with the reason for one corrective request.
    """
    keys = {_title_key(title) for title in taken}
    reasoning = reasoning_cfg(reasoning_on, reasoning_prefill)
    messages: list[ChatMessage] = list(request)
    for attempt in range(2):
        raw = await _ask(client, base, settings, messages, reasoning=reasoning, kv_tracker=kv_tracker)
        title = _clean(raw)
        reason = _rejection(raw, title, keys)
        if reason is None:
            return title
        if attempt == 0:
            retry: list[ChatMessage] = [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": RETRY_INSTRUCTIONS.format(reason=reason)},
            ]
            messages += retry
    return None
