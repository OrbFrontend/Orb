"""Routes for library tag vocabulary management and auto-tagging."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from ...core import agent_lane_max_tokens, scrub_log
from ...database import (
    VocabularyConflict,
    add_dismissals,
    apply_auto_tags,
    apply_avatar_dhash,
    delete_character_card,
    get_auto_tag_counts,
    get_card_activity,
    get_character_card,
    get_dismissals,
    get_relink_impact,
    get_settings,
    get_vocabulary,
    get_world,
    list_cards_for_dedupe,
    list_pending_auto_tag_ids,
    list_stale_avatar_ids,
    read_avatar_b64,
    relink_card,
    remove_dismissals,
    replace_vocabulary,
)
from ...features.library_dedupe import (
    DEDUPE_REVISION,
    body_hash,
    dhash_from_image_bytes,
    find_duplicates,
    signals_for_all,
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
from ..schemas import (
    AutoTagRunRequest,
    DuplicateDismissRequest,
    DuplicateResolveRequest,
    LibraryTagVocabulary,
)

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


@router.post("/api/library/duplicates/scan")
async def api_scan_library_duplicates(request: Request):
    """Rebuild the server-side duplicate report and stream avatar-cache progress."""
    abort_token = AbortToken()

    async def _gen():
        if _run_lock.locked():
            yield {
                "event": "error",
                "data": "The library is busy — a tagging run or another duplicate scan is already under way",
            }
            return

        async with _run_lock:
            stamp_prefix = f"{DEDUPE_REVISION}:"
            stale_ids = await list_stale_avatar_ids(stamp_prefix)
            total = len(stale_ids)
            yield {"event": "start", "data": {"total": total}}

            for done, card_id in enumerate(stale_ids, start=1):
                if abort_token.is_aborted:
                    return
                avatar = await read_avatar_b64(card_id)
                if avatar is None:  # deleted during the scan
                    continue
                avatar_b64, updated_at, name = avatar
                try:
                    image_bytes = base64.b64decode(avatar_b64 or "", validate=True)
                except (binascii.Error, ValueError):
                    image_bytes = b""
                dhash = dhash_from_image_bytes(image_bytes) if image_bytes else ""
                await apply_avatar_dhash(card_id, dhash, f"{stamp_prefix}{updated_at}")
                yield {"event": "progress", "data": {"done": done, "total": total, "name": name}}

            # Re-read after cache updates. Text is purposefully fresh on every
            # scan; only avatar decoding is expensive enough to cache.
            cards = await list_cards_for_dedupe()
            dhashes = {
                str(card["id"]): str(card["avatar_dhash"])
                for card in cards
                if str(card["avatar_dhash_stamp"]) == f"{stamp_prefix}{card['updated_at']}"
            }
            report = find_duplicates(signals_for_all(cards, dhashes), dismissed=await get_dismissals())
            # Card bodies never leave this endpoint. The UI receives only names
            # to label result IDs; compare is the two-card body boundary.
            report["cards"] = [{"id": str(card["id"]), "name": str(card["name"])} for card in cards]
            report["stats"]["avatar_rehashed"] = total
            yield {"event": "done", "data": report}

    return _CleanupStreamingResponse(
        _sse_stream(_gen(), request, abort_token=abort_token),
        media_type="text/event-stream",
    )


async def _compare_card_payload(card_id: str, activity: dict[str, dict]) -> dict | None:
    """The compare endpoint's one-card view, without its avatar blob."""
    card = await get_character_card(card_id)
    if card is None:
        return None
    # World labels are intentionally resolved here rather than in the list path:
    # the compare route is the only duplicate route that needs them.
    world_name = None
    world_id = card.get("world_id")
    if world_id:
        world = await get_world(str(world_id))
        world_name = world.get("name") if world else None
    return {
        "card": card,
        "activity": activity.get(
            card_id, {"solo": 0, "active_groups": 0, "historical_groups": 0, "total": 0, "last_used_at": None}
        ),
        "world_name": world_name,
    }


@router.get("/api/library/duplicates/compare")
async def api_compare_library_duplicates(a: str, b: str):
    """Return full bodies for exactly two cards and keeper-selection evidence."""
    if not a or not b or a == b:
        raise HTTPException(status_code=422, detail="Compare two different character cards")
    activity = await get_card_activity([a, b])
    left, right = await asyncio.gather(_compare_card_payload(a, activity), _compare_card_payload(b, activity))
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="Character card not found")
    impact = await get_relink_impact(a, b)
    return {"a": left, "b": right, "shared_group_collisions": impact["collisions"]}


async def _dismissal_stamps(pairs: list[tuple[str, str]]) -> list[tuple[str, str, str, str]]:
    """Read just-dismissed cards and stamp pairs with their meaningful bodies."""
    ids = sorted({card_id for pair in pairs for card_id in pair})
    cards = await asyncio.gather(*(get_character_card(card_id) for card_id in ids))
    by_id = {card_id: card for card_id, card in zip(ids, cards, strict=True)}
    missing = next((card_id for card_id, card in by_id.items() if card is None), None)
    if missing is not None:
        raise HTTPException(status_code=404, detail="Character card not found")
    return [(a, b, body_hash(by_id[a] or {}), body_hash(by_id[b] or {})) for a, b in pairs]


@router.post("/api/library/duplicates/dismiss")
async def api_dismiss_library_duplicates(data: DuplicateDismissRequest):
    """Remember intentional variants until either card's meaningful body changes."""
    values = await _dismissal_stamps(data.pairs)
    return {"dismissed": await add_dismissals(values)}


@router.delete("/api/library/duplicates/dismiss")
async def api_restore_library_duplicates(data: DuplicateDismissRequest):
    """Undo a deliberate-variant dismissal."""
    return {"restored": await remove_dismissals(data.pairs)}


@router.post("/api/library/duplicates/resolve")
async def api_resolve_library_duplicate(data: DuplicateResolveRequest):
    """Delete a duplicate, blocking history loss unless the caller chooses relink."""
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="The library is busy; wait for the current run to finish")
    async with _run_lock:
        keeper, doomed = await asyncio.gather(get_character_card(data.keep_id), get_character_card(data.remove_id))
        if keeper is None or doomed is None:
            raise HTTPException(status_code=404, detail="Character card not found")
        impact = await get_relink_impact(data.remove_id, data.keep_id)
        if impact["conversations"] and not data.relink:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This card has conversations. Relink them to the keeper before deleting it.",
                    "impact": impact,
                },
            )
        if data.relink:
            impact = await relink_card(data.remove_id, data.keep_id)
        if not await delete_character_card(data.remove_id):
            raise HTTPException(status_code=404, detail="Character card not found")
        return {"ok": True, "relinked": data.relink, "impact": impact}
