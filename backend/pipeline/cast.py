"""cast.py -- who speaks: speaking-plan validation and round-robin policy.

**"Beat" here is the Director's one-line note for a single speaker** -- a story
beat, the screenwriting sense of the word, and the only place in group chats that
still uses it. The turn unit a whole group request produces is an *exchange*
(``messages.exchange_id``); the wider unit the sheet pass and the image-gen
subject order read -- the user's last message and every reply since -- is a
*round*. Three units, three words, none of them interchangeable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# What may sit between a resolved speaker and its beat. Applied to the text
# *after* the speaker has already been identified, never to find the speaker:
# a `-` is as much a part of `jean-luc-picard` as of `alice-hart`, and every
# multi-word display name produces a hyphenated speaker key, so splitting on it
# to locate the boundary discarded the whole plan for most casts.
_PLAN_SEPARATOR = re.compile(r"\A\s*[—–:-]?\s*")

# The label must end where the beat begins. Without this, a member keyed `aria`
# would claim a plan line naming `arianna`.
_PLAN_BOUNDARY = re.compile(r"\A[\s—–:-]")


def _resolve_item(text: str, labels: Sequence[tuple[str, Mapping]]) -> tuple[Mapping, str] | None:
    """Match one plan line against the known speaker labels, longest first."""
    low = text.casefold()
    for label, member in labels:
        rest = text[len(label) :]
        if low.startswith(label) and (not rest or _PLAN_BOUNDARY.match(rest)):
            return member, _PLAN_SEPARATOR.sub("", rest).strip()
    return None


def parse_speaking_plan(raw: object, members: Sequence[Mapping], cap: int) -> list[tuple[Mapping, str]] | None:
    """Validate a Director plan. None means malformed/missing; [] is intentional rest.

    Each line is ``<speaker_key> — <beat>`` (the shape ``build_direct_scene_override``
    asks for), but the speaker is found by matching the roster's own keys and display
    names against the head of the line rather than by splitting on punctuation — a
    speaker key is kebab-cased, so it contains the very characters a split would
    treat as the boundary.
    """
    if raw is None or not isinstance(raw, list):
        return None
    eligible = [m for m in members if m.get("active") and not m.get("muted")]
    # Longest label first, so `alice-hart` is never truncated to a shorter
    # member's `alice` and a key always wins over a name that is its prefix.
    labels = sorted(
        (
            (label, m)
            for m in eligible
            for field in ("speaker_key", "display_name")
            if (label := str(m.get(field) or "").casefold())
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    out: list[tuple[Mapping, str]] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        resolved = _resolve_item(item.strip(), labels)
        if resolved is None:
            continue
        member, beat = resolved
        if not out or out[-1][0]["id"] != member["id"]:
            out.append((member, beat))
        if len(out) >= cap:
            break
    return out if out or not raw else None


def round_robin_member(members: Sequence[Mapping], messages: Sequence[Mapping]) -> Mapping | None:
    eligible = [m for m in members if m.get("active") and not m.get("muted")]
    if not eligible:
        return None
    last_id = next((m.get("speaker_member_id") for m in reversed(messages) if m.get("speaker_member_id")), None)
    for index, member in enumerate(eligible):
        if member["id"] == last_id:
            return eligible[(index + 1) % len(eligible)]
    return eligible[0]
