"""Routes for library tag vocabulary management and auto-tagging."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from ...core import agent_lane_max_tokens, scrub_log
from ...database import (
    apply_auto_tags,
    bump_auto_tag_vocab_hash,
    count_library_cards,
    count_pending_auto_tags,
    count_tagged_cards,
    get_character_card,
    get_settings,
    get_vocabulary,
    list_pending_auto_tag_ids,
    prune_auto_tags,
    set_vocabulary,
)
from ...features.library_tags import (
    AutoTagUnavailable,
    build_system_prompt,
    build_tag_tool,
    diff_vocabulary,
    normalize_vocabulary,
    tag_card,
    vocabulary_hash,
)
from ...inference import (
    AbortToken,
    LLMCallError,
    agent_lane_from_settings,
    client_from_settings,
)
from ..deps import _CleanupStreamingResponse, _sse_stream
from ..schemas import AutoTagRunRequest, LibraryTagVocabulary

logger = logging.getLogger(__name__)

router = APIRouter()

# A vocabulary save and a tagging run both write character-card tags.
_run_lock = asyncio.Lock()

_MAX_CONSECUTIVE_FAILURES = 5

_MAX_TOKENS_FLOOR = 512


async def _tag_state() -> dict:
    """Return the vocabulary and counts used by the manager panel."""
    vocabulary = await get_vocabulary()
    pending = await count_pending_auto_tags(vocabulary_hash(vocabulary)) if vocabulary else 0
    return {
        "vocabulary": vocabulary,
        "total": await count_library_cards(),
        "pending": pending,
        "tagged": await count_tagged_cards(),
    }


@router.get("/api/library/tags")
async def api_get_library_tags():
    return await _tag_state()


@router.put("/api/library/tags")
async def api_put_library_tags(data: LibraryTagVocabulary):
    """Persist the vocabulary and update existing card assignments."""
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A tagging run is in progress")
    async with _run_lock:
        old = await get_vocabulary()
        new = normalize_vocabulary(data.vocabulary)
        added, removed = diff_vocabulary(old, new)

        await set_vocabulary(new)
        if removed:
            await prune_auto_tags(removed)
        if not added:
            await bump_auto_tag_vocab_hash(vocabulary_hash(new))
        return await _tag_state()


@router.post("/api/library/auto-tag/run")
async def api_run_auto_tag(data: AutoTagRunRequest, request: Request):
    """Tag pending cards sequentially and stream progress as SSE."""
    settings = await get_settings()
    abort_token = AbortToken()

    async def _gen():
        if _run_lock.locked():
            yield {
                "event": "error",
                "data": "The library is busy — a tagging run or a vocabulary save is already under way",
            }
            return
        async with _run_lock:
            vocabulary = await get_vocabulary()
            if not vocabulary:
                yield {"event": "error", "data": "Add at least one tag to the vocabulary first"}
                return

            vocab_hash = vocabulary_hash(vocabulary)
            pending = await list_pending_auto_tag_ids(vocab_hash)
            total = len(pending)
            yield {"event": "start", "data": {"total": total}}

            # Keep the system message and tool schema identical across cards.
            system = build_system_prompt(vocabulary)
            tool = build_tag_tool(vocabulary)
            client = client_from_settings(settings, abort_token=abort_token)
            agent_client, model = agent_lane_from_settings(settings, writer_client=client, abort_token=abort_token)
            max_tokens = agent_lane_max_tokens(settings, floor=_MAX_TOKENS_FLOOR)

            tagged = 0
            failed = 0
            consecutive = 0
            for done, card_id in enumerate(pending, start=1):
                if abort_token.is_aborted:
                    return
                card = await get_character_card(card_id)
                if card is None:  # deleted mid-run
                    continue
                try:
                    tags = await tag_card(
                        agent_client,
                        model or "",
                        card,
                        vocabulary=vocabulary,
                        system=system,
                        tool=tool,
                        max_tokens=max_tokens,
                        reasoning_on=data.reasoning,
                    )
                except (AutoTagUnavailable, LLMCallError) as e:
                    failed += 1
                    consecutive += 1
                    logger.warning("Auto-tag failed for card %s: %s", scrub_log(card_id), e)
                    yield {
                        "event": "card_error",
                        "data": {"done": done, "total": total, "name": str(card.get("name") or ""), "error": str(e)},
                    }
                    if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                        yield {
                            "event": "error",
                            "data": f"Stopped after {consecutive} failures in a row — check the Agent endpoint",
                        }
                        return
                    continue

                consecutive = 0
                await apply_auto_tags(card_id, tags, vocab_hash, str(card.get("updated_at") or ""))
                tagged += 1
                yield {
                    "event": "progress",
                    "data": {"done": done, "total": total, "name": str(card.get("name") or ""), "tags": tags},
                }

            yield {"event": "done", "data": {"tagged": tagged, "failed": failed}}

    return _CleanupStreamingResponse(
        _sse_stream(_gen(), request, abort_token=abort_token),
        media_type="text/event-stream",
    )
