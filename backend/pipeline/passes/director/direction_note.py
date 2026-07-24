"""
passes/director/direction_note.py -- Direction-note step.

Asks the model, via a forced ``record_direction_note`` call, whether anything from
this turn should persist for the rest of the branch. Gated by the master Writing
switch and the enabled ``field_type='direction_note'`` fragments whose timing matches
this placement; each filled parameter becomes one labelled note (empty when nothing is
worth recording). Issues one call for the whole timing group, or one per fragment when
the per-fragment director toggle is on.

The wire schema in the shared per-turn tool blob is the union of every direction-note
fragment, held byte-stable so every call reuses the cached base and only forces the
tool choice. A call narrows the request text and the extraction to its own fragments --
the timing group, or a single fragment when the per-fragment toggle splits the group.
The trailing depends on placement: the post-turn placement replays the writer's user
message and reply to extend the warm writer/editor prefix; the pre-writer placement
appends only the request, carrying this turn's scene direction inside it.

Errors and aborts are swallowed into an empty result. The post-turn placement runs
immediately before the turn's ``_result`` is emitted, so a propagating exception
would skip persistence of the finished reply -- recording a note must never do that.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....inference import (
    RECORD_DIRECTION_NOTE_CHOICE,
    CachedBase,
    LLMClient,
    build_direction_note_prompt,
    build_direction_note_tool,
    parse_tool_calls,
    reasoning_cfg,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DirectionNoteResult:
    """Typed result of the direction-note step, yielded as the ``done`` payload.

    ``notes`` holds one ``{interactive_fragment_id, interactive_fragment_label, content}`` row per filled
    category parameter; empty when nothing is worth recording.
    """

    notes: list[dict] = field(default_factory=list)
    agent_raw: str = ""


def extract_direction_notes(
    tool_calls: list[dict],
    direction_note_fragments: Sequence[Mapping[str, Any]],
) -> list[dict]:
    """Turn parsed ``record_direction_note`` calls into labelled note rows.

    Each filled parameter is keyed by a direction-note fragment's id and becomes one note
    carrying that fragment's label, denormalised so a later rename or deletion of the
    fragment cannot orphan the note's heading. Parameters for unknown ids and blank or
    non-string values are dropped (a malformed model reply records nothing); a later
    call wins on key collisions.
    """
    labels = {df["id"]: (df.get("injection_label") or df.get("label") or df["id"]) for df in direction_note_fragments}
    values: dict[str, str] = {}
    for tc in tool_calls:
        if tc.get("name") == "record_direction_note":
            for k, v in (tc.get("arguments", {}) or {}).items():
                if k in labels and isinstance(v, str) and v.strip():
                    values[k] = v.strip()
    return [
        {"interactive_fragment_id": fid, "interactive_fragment_label": labels[fid], "content": c} for fid, c in values.items()
    ]


async def direction_note_step(
    client: LLMClient,
    base: CachedBase,
    *,
    settings: Mapping[str, Any],
    direction_note_fragments: Sequence[Mapping[str, Any]],
    active_notes: Sequence[Mapping[str, Any]],
    placement: str,
    inj_block: str | None = None,
    reply_text: str | None = None,
    writer_user_msg: str | list[ContentPart] | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield reasoning chunks during the call(s), then a single done dict.

    One forced ``record_direction_note`` call for the whole group, or one per fragment
    when the per-fragment director toggle is on; notes from every call are combined.

    Yields:
        ``{"type": "reasoning", "delta": str}``
        ``{"type": "done", "result": DirectionNoteResult}``
    """
    if not direction_note_fragments:
        yield {"type": "done", "result": DirectionNoteResult()}
        return

    # The per-fragment director toggle gives each category its own call so its attention is
    # not split across the others; off, the whole group shares one call. The split is
    # request-text-only -- the wire schema stays the shared-base union, so the extra calls
    # reuse the cached prefix rather than busting it. Every call sees only the prior-branch
    # notes: this turn's sibling notes are deliberately not fed forward, which would re-mix
    # the categories the split just separated.
    per_fragment_on = bool(settings.get("director_individual_fragments", 0))
    groups = [[df] for df in direction_note_fragments] if per_fragment_on else [list(direction_note_fragments)]

    hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.4, "max_tokens": 2048})

    notes: list[dict] = []
    raws: list[str] = []
    for group in groups:
        if client.is_aborted:
            break

        request = build_direction_note_prompt(
            active_notes,
            group,
            inj_block=inj_block if placement == "pre_writer" else None,
            reasoning_on=reasoning_on,
            tool_schema=build_direction_note_tool(group),
        )
        if placement == "post_turn":
            # Replay the writer exchange so each call extends the warm writer/editor prefix.
            trailing: list[ChatMessage] = [
                {"role": "user", "content": writer_user_msg or ""},
                {"role": "assistant", "content": reply_text or ""},
                {"role": "user", "content": request},
            ]
        else:
            trailing = [{"role": "user", "content": request}]

        resp: dict = {}
        try:
            async for event in base.complete(
                client,
                label="direction_note",
                trailing=trailing,
                tool_choice=RECORD_DIRECTION_NOTE_CHOICE,
                kv_tracker=kv_tracker,
                **hyperparams,
                **reasoning_cfg(reasoning_on, reasoning_prefill),
            ):
                if event["type"] == "reasoning":
                    yield {"type": "reasoning", "delta": event["delta"]}
                elif event["type"] == "done":
                    resp = event["message"]
        except Exception:
            # A failed call records nothing for its group but must neither drop the groups
            # already recorded nor propagate: the post-turn placement runs just before the
            # turn's ``_result``, where an exception would skip persisting the finished reply.
            logger.exception("Direction-note call failed; skipping this group")
            continue

        raw = json.dumps(resp, default=str)
        logger.info("Direction-note step output:\n%s", raw)
        raws.append(raw)
        notes.extend(extract_direction_notes(parse_tool_calls(resp), group))

    yield {"type": "done", "result": DirectionNoteResult(notes=notes, agent_raw="\n".join(raws))}
