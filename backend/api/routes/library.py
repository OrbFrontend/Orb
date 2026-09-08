"""Routes for library tag vocabulary management and auto-tagging."""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from ...core import agent_lane_max_tokens, scrub_log
from ...database import (
    VocabularyConflict,
    apply_auto_tags,
    get_auto_tag_counts,
    get_character_card,
    get_settings,
    get_vocabulary,
    list_pending_auto_tag_ids,
    replace_vocabulary,
)
from ...features.library_tags import (
    AutoTagUnavailable,
    build_system_prompt,
    build_tag_tool,
    normalize_vocabulary,
    tag_card,
    vocabulary_hash,
    vocabulary_revision,
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
    counts = await get_auto_tag_counts(vocabulary_hash(vocabulary) if vocabulary else None)
    return {
        "vocabulary": vocabulary,
        "revision": vocabulary_revision(vocabulary),
        **counts,
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
        if data.base_revision != vocabulary_revision(old):
            raise HTTPException(status_code=409, detail="The tag vocabulary changed in another window; review it and try again")
        try:
            cards_changed = await replace_vocabulary(new, expected=old, new_hash=vocabulary_hash(new))
        except VocabularyConflict as exc:
            raise HTTPException(status_code=409, detail=f"{exc}; review it and try again") from None
        state = await _tag_state()
        state["cards_changed"] = cards_changed
        return state


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
            pending = await list_pending_auto_tag_ids(vocab_hash, force=data.force)
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
                except AutoTagUnavailable as e:
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
                except LLMCallError as e:
                    failed += 1
                    logger.warning("Auto-tag endpoint failed for card %s: %s", scrub_log(card_id), e.sentence or e)
                    yield {
                        "event": "card_error",
                        "data": {
                            "done": done,
                            "total": total,
                            "name": str(card.get("name") or ""),
                            "error": e.sentence or "The Agent endpoint failed",
                        },
                    }
                    yield {
                        "event": "error",
                        "data": "The Agent endpoint failed after its retries; the remaining cards were not sent",
                    }
                    return
                except httpx.HTTPError as e:
                    failed += 1
                    logger.warning("Auto-tag transport failed for card %s: %s", scrub_log(card_id), e)
                    yield {
                        "event": "card_error",
                        "data": {
                            "done": done,
                            "total": total,
                            "name": str(card.get("name") or ""),
                            "error": "The Agent endpoint could not be reached",
                        },
                    }
                    yield {
                        "event": "error",
                        "data": "The Agent endpoint could not be reached after its retries; the remaining cards were not sent",
                    }
                    return

                consecutive = 0
                applied = await apply_auto_tags(card_id, tags, vocab_hash, str(card.get("updated_at") or ""))
                if not applied:
                    failed += 1
                    yield {
                        "event": "card_error",
                        "data": {
                            "done": done,
                            "total": total,
                            "name": str(card.get("name") or ""),
                            "error": "The card changed while it was being tagged; it was left pending",
                        },
                    }
                    continue
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
