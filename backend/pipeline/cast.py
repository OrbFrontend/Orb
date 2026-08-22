"""cast.py -- who speaks: speaking-plan validation and round-robin policy.

**A "cue" is what the Director tells a single speaker to do in one reply** --
one line, in the theatrical sense. This module owns the term; it travels from
here into the writer's tail (``## Your cue``) and onto the wire as the ``cue``
field of ``speaking_plan`` and ``speaker_start``. It is not a unit of time at
all. The turn unit a whole group request produces is an *exchange*
(``messages.exchange_id``); the wider unit the sheet pass and the image-gen
subject order read -- the user's last message and every reply since -- is a
*round*. Three words, none of them interchangeable.

It was called a *beat* until the word started surfacing in the writer's own
prose: "paused a beat" is idiomatic narration, so a section header naming it
reinforced a word the model already reaches for. A cue is rare in narration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# What may sit between a resolved speaker and its cue. Applied to the text
# *after* the speaker has already been identified, never to find the speaker:
# a `-` is as much a part of `jean-luc-picard` as of `alice-hart`, and every
# multi-word display name produces a hyphenated speaker key, so splitting on it
# to locate the boundary discarded the whole plan for most casts.
_PLAN_SEPARATOR = re.compile(r"\A\s*[—–:-]?\s*")

# The label must end where the cue begins. Without this, a member keyed `aria`
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

    Each line is ``<speaker_key> — <cue>`` (the shape ``build_direct_scene_override``
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
        member, cue = resolved
        if not out or out[-1][0]["id"] != member["id"]:
            out.append((member, cue))
        if len(out) >= cap:
            break
    return out if out or not raw else None


def plan_cue(raw: object, members: Sequence[Mapping], member_id: str) -> str:
    """The Director's cue for one member, for the paths that cast without the plan.

    A pin (regenerate, magic rewrite, ``/speak``, a manual pick) and round-robin
    both settle *who* speaks before the plan is read, and must: the row a
    regenerate replaces already owns its speaker, and swapping it would hand one
    message's branch siblings to two different characters. That is a casting
    decision and nothing more -- if the Director wrote a cue for the very member
    about to speak, that cue is what it wants from this reply, and dropping it wrote
    the speaker blind against a scene direction the Director had already aimed
    somewhere else.

    Read **uncapped** on purpose. ``group_max_speakers`` bounds how many members
    answer an exchange; here nobody is being cast, so a plan longer than the cap
    must still yield the cue of the member who *is* speaking.
    """
    if not isinstance(raw, list):
        return ""
    for member, cue in parse_speaking_plan(raw, members, len(raw)) or ():
        if str(member["id"]) == str(member_id):
            return cue
    return ""


def round_robin_member(members: Sequence[Mapping], messages: Sequence[Mapping]) -> Mapping | None:
    eligible = [m for m in members if m.get("active") and not m.get("muted")]
    if not eligible:
        return None
    last_id = next((m.get("speaker_member_id") for m in reversed(messages) if m.get("speaker_member_id")), None)
    for index, member in enumerate(eligible):
        if member["id"] == last_id:
            return eligible[(index + 1) % len(eligible)]
    return eligible[0]
