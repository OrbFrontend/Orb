from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# Splits `<speaker> — <beat>` on any of the three dashes/colon a model reaches
# for; the beat itself may contain more of them, hence maxsplit=1.
_PLAN_ITEM = re.compile(r"\s*(?:—|:|-)\s*")


def parse_speaking_plan(raw: object, members: Sequence[Mapping], cap: int) -> list[tuple[Mapping, str]] | None:
    """Validate a Director plan. None means malformed/missing; [] is intentional rest."""
    if raw is None or not isinstance(raw, list):
        return None
    by_key = {str(m["speaker_key"]).casefold(): m for m in members if m.get("active") and not m.get("muted")}
    by_name = {str(m["display_name"]).casefold(): m for m in members if m.get("active") and not m.get("muted")}
    out: list[tuple[Mapping, str]] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        bits = _PLAN_ITEM.split(item, maxsplit=1)
        who = bits[0].strip().casefold()
        beat = bits[1].strip() if len(bits) > 1 else ""
        member = by_key.get(who) or by_name.get(who)
        if member and (not out or out[-1][0]["id"] != member["id"]):
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
