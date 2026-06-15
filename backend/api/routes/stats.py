"""Aggregate usage statistics for the homepage stat grid."""

from __future__ import annotations

import os
import random

from fastapi import APIRouter

from ...core import estimate_tokens
from ...database import DB_PATH, get_generated_chars, get_global_stats

router = APIRouter()


@router.get("/api/stats")
async def api_global_stats():
    """Aggregate usage statistics for the homepage stat grid."""
    s = await get_global_stats()
    # Persistent lifetime counter: seeded from existing messages on first use,
    # then incremented per successful generation -- not recomputed per call.
    generated_chars = await get_generated_chars()
    avg_latency = s["avg_latency_ms"]
    # On-disk footprint: the main db plus its WAL/shared-memory sidecars, which
    # hold not-yet-checkpointed pages and can be a sizable share of the total.
    storage_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    # The hero slot shows one of several "story beat" themes, chosen uniformly
    # among those with data (50/50 today, extensible by appending more themes).
    themes = []
    if s["favorite_character"]:
        themes.append(("favorite", s["favorite_character"]))
    if s["missed_character"]:
        themes.append(("missed", s["missed_character"]))
    spotlight = None
    if themes:
        theme, card = random.choice(themes)
        spotlight = {"theme": theme, **card}
    return {
        "total_conversations": s["total_conversations"],
        "total_messages": s["total_messages"],
        "character_spotlight": spotlight,
        "total_words": round(s["user_chars"] / 5),
        "estimated_tokens": estimate_tokens(generated_chars),
        "storage_bytes": storage_bytes,
        "avg_latency_ms": round(avg_latency) if avg_latency is not None else None,
    }
