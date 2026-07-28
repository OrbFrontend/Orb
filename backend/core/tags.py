"""Canonical host normalization for an already-supplied character-tag list.

This is not the character-card feature. It owns only the write invariant for a
tag value supplied by a caller. Card persistence, import, filtering, UI, and
extension operations remain in their owning layers.

Until this module existed there was no server-side rule at all:
``CharacterCardUpdate.tags`` is an unconstrained ``list[str] | None`` and the
update query wrote ``json.dumps(...)`` straight through. The only rules lived
in the frontend chip widget, which the import path and any direct API call
bypass entirely.

That was survivable while humans typed every tag. It stops being survivable
once ``card.tags.set`` lets an extension write them, because tag filtering
matches *exact strings*: a human-entered ``"Noir"`` and an extension-written
``"noir"`` become two filter chips over disjoint subsets of the same library.
An extension that normalized against a host that did not would manufacture the
exact disorder a tag-standardizing package is installed to remove.

So both paths call this, and the casing question is settled here rather than
per caller: **dedupe is case-insensitive, and the first spelling wins.** The
chip widget was case-sensitive; it was changed to match, because two rules that
disagree are worse than either rule.

Over-long tags are clipped rather than dropped. A clipped tag is wrong; a
dropped one is invisible, and the failure a user can see is the one they can
fix. Clipping happens on a character boundary, so the result is always valid
UTF-8 even when the cut lands mid-codepoint.

Deliberately *not* applied on import. Exported PNGs keep author fidelity, and
existing rows are never backfilled -- normalization is a rule about writes, not
a migration.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MAX_TAG_BYTES = 64
"""Canonical host storage limit for one tag, in UTF-8 bytes."""

MAX_TAGS_PER_CARD = 32
"""Canonical host storage limit for the number of tags on one card."""


def _clip(text: str, limit: int = MAX_TAG_BYTES) -> str:
    """Clip to *limit* UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def normalize_tags(raw: Any) -> list[str]:
    """Normalize a tag list: trim, clip, drop empties, dedupe, cap the count.

    Total over any input. A non-list, or a list holding non-strings, yields the
    tags it can and silently ignores the rest -- this runs on the write path of
    an API that has always accepted whatever it was handed, and rejecting a
    payload that used to be accepted would break card editing to enforce a rule
    about tags.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = _clip(item.strip()).strip()
        if not tag:
            continue
        folded = tag.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        normalized.append(tag)
        if len(normalized) >= MAX_TAGS_PER_CARD:
            break
    return normalized


def tags_equal(left: Iterable[str], right: Iterable[str]) -> bool:
    """Whether two tag lists normalize to the same stored value."""
    return normalize_tags(list(left)) == normalize_tags(list(right))
