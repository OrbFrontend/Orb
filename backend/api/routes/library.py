"""Library-wide maintenance: the curated tag vocabulary and the auto-tag run.

Deliberately not part of the conversation plumbing. The run is a batch job over
the card table with its own storage, its own prefix, and its own single-flight
lock — no conversation id, no pipeline import, nothing registered in the shared
tool catalog. See ``features/library_tags`` for the same standing constraint.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from ...core import agent_lane_max_tokens, scrub_log
from ...database import (
    bump_auto_tag_vocab_hash,
    count_library_cards,
    count_pending_auto_tags,
    get_auto_tag_assignments,
    get_character_card,
    get_settings,
    get_vocabulary,
    list_pending_auto_tag_ids,
    prune_auto_tags,
    set_vocabulary,
    upsert_auto_tags,
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

# Module-local single-flight, NOT the conversation stream lock: a tagging run has
# no conversation, and registering it in ``_active_aborts`` would put a batch job
# in the registry ``/stop`` walks. One run at a time across the whole process,
# because two concurrent runs would race the same rows and double the bill.
_run_lock = asyncio.Lock()

# A dead endpoint should cost a handful of calls, not four hundred. Consecutive,
# so a library with a few cards the model chokes on still finishes.
_MAX_CONSECUTIVE_FAILURES = 5

# Enough for a dozen short tag strings plus a reasoning model's preamble.
_MAX_TOKENS_FLOOR = 512


async def _tag_state() -> dict:
    """The one shape both the Manager panel and the library filter read.

    One endpoint for both because they are one fact: the panel needs the counts
    to label its button, and the browser needs the vocabulary and the assignments
    to build its chip row and its filter. Splitting them would cost the modal a
    second round trip to learn the same thing.
    """
    vocabulary = await get_vocabulary()
    return {
        "vocabulary": vocabulary,
        "total": await count_library_cards(),
        "pending": await count_pending_auto_tags(vocabulary_hash(vocabulary)),
        "assignments": await get_auto_tag_assignments(),
    }


@router.get("/api/library/tags")
async def api_get_library_tags():
    return await _tag_state()


@router.put("/api/library/tags")
async def api_put_library_tags(data: LibraryTagVocabulary):
    """Persist the vocabulary and turn the diff into exactly the right work.

    Deleting a tag is free: no card can have gained a tag it was never offered,
    so the stored answers stay correct once the deleted name is stripped, and the
    hash bump marks them current. Adding one leaves every row on its old hash,
    which is what makes every card pending — a full re-pass, as it must be.
    """
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
    """Tag every pending card, one forced call each, committing as it goes (SSE).

    Per-card commits are what make the run resumable: a cancel, a dropped
    connection, or a closed modal keeps everything already answered, and the next
    press picks up the rest. Cancellation therefore needs no endpoint — the
    client aborts the fetch, ``_sse_stream``'s disconnect watcher fires the abort
    token, and the in-flight call dies.

    ``reasoning`` is constant for the whole run, so pinning it does not disturb
    the single shared prefix every card's call rides on.
    """
    settings = await get_settings()
    abort_token = AbortToken()

    async def _gen():
        if _run_lock.locked():
            yield {"event": "error", "data": "A tagging run is already in progress"}
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

            # The run's shared prefix, built once: identical system message and
            # identical tools blob on every call, so each one after the first
            # hits the provider's prefix cache on the whole instruction block.
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
                if card is None:  # deleted mid-run; its row cascaded away with it
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
                    # No row is written, so the card stays pending and the next
                    # run retries it. Non-fatal: one bad card must not end a run
                    # the user is watching make progress.
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
                await upsert_auto_tags(card_id, tags, vocab_hash, str(card.get("updated_at") or ""))
                tagged += 1
                yield {
                    "event": "progress",
                    "data": {"done": done, "total": total, "name": str(card.get("name") or ""), "tags": tags},
                }

            yield {"event": "done", "data": {"tagged": tagged, "failed": failed}}

    return _CleanupStreamingResponse(
        # cid=None on purpose: that argument is the *conversation* stream lock and
        # the abort registry keyed by conversation id. Single-flight is _run_lock
        # above; the disconnect watcher works without a cid.
        _sse_stream(_gen(), request, abort_token=abort_token),
        media_type="text/event-stream",
    )
