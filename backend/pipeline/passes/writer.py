"""
passes/writer.py — The writer pass: streams the main story response.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ...core import (
    CastMember,
    ChatMessage,
    ContentPart,
    GroupContextMode,
    Macros,
    build_multimodal_content,
    extract_hyperparams,
)
from ...inference import (
    CachedBase,
    LLMClient,
    _KVCacheTracker,
    member_macros,
    reasoning_cfg,
    tail_carries_identity,
)
from .editor.length_guard import LengthGuard, writer_nudge

if TYPE_CHECKING:
    from ..state import TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


def strip_speaker_label(text: str, speaker_name: str) -> str:
    """Remove a leading plain/Markdown label for the complete speaker name.

    A **colon** is what makes a label a label -- ``Alice smiled.`` is prose and
    always was, and ``**Alice** walked to the door.`` is the same sentence in
    bold. The one colon-free form accepted is a name that owns its whole line
    (a heading, or bold with nothing after it), which prose never is.
    """
    if not text or not speaker_name.strip():
        return text
    name = re.escape(speaker_name.strip())
    emph = r"(?:\*\*|__)"
    label = re.compile(
        rf"\A[ \t]*(?:"
        # **Alice:** / **Alice**: / __Alice__:
        rf"{emph}\s*{name}\s*:\s*{emph}[ \t]*:?[ \t]*"
        rf"|{emph}\s*{name}\s*{emph}[ \t]*:[ \t]*"
        # **Alice** alone on its line -- no colon, but nothing follows it either.
        rf"|{emph}\s*{name}\s*{emph}[ \t]*(?=\r?\n)"
        rf"|\[\s*{name}\s*\]\s*:[ \t]*"
        rf"|\#{{1,6}}[ \t]+{name}\s*:?[ \t]*(?:\r?\n)?"
        rf"|{name}\s*:[ \t]*"
        rf")[ \t]*(?:\r?\n)?",
        re.IGNORECASE,
    )
    return label.sub("", text, count=1)


# Private perspective is the one mode that puts a speaker's own sheet *after*
# history (``group_context.tail_carries_identity``). Every other mode, and every
# solo turn, reads it from the system body *before* the transcript. That
# inversion falls out of the cache layout rather than intent: Private keeps the
# shared body speaker-independent, so the speaking card has nowhere to go but
# the tail. The cost is that a fixed, present-tense sheet becomes the last
# identity text the model reads before writing, outranking a transcript that has
# since changed the character's hair, dress or gear. One line restores the
# reading order the placement destroys. It is billed on every writer and editor
# call, so it stays one sentence.
#
# It deliberately does not date the sheet. `group_sheet_updates` can bring it
# current mid-scene, and "from the scene's start" would then be a false claim
# about the very text the user had just approved — telling the model to discount
# the update rather than the drift. The transcript still wins either way, which
# is the only thing this line has to establish.
SHEET_FRAMING = (
    "Reference sheet for this scene. Where the transcript above shows it has changed "
    "— appearance, dress, injuries, what they carry — follow the transcript."
)


def build_writer_content(
    lorebook_block: str,
    inj_block: str,
    tools_sent: bool,
    effective_msg: str,
    attachments: Sequence[Mapping[str, Any]] | None,
    length_guard: LengthGuard | None,
    depth_block: str = "",
    speaker: CastMember | None = None,
    speaker_beat: str = "",
    macros: Macros | None = None,
    prevent_prompt_overrides: bool = False,
    context_mode: GroupContextMode = "private",
) -> str | list[ContentPart]:
    """Build the writer's user-message content (string or multimodal list).

    Built once and threaded into both the writer pass and the editor, which
    replays it verbatim to extend the writer's KV-cached prefix. The length-guard
    nudge (preventive arm) fires only in enforce mode; a non-None *length_guard*
    already means the feature is enabled.

    *context_mode* decides whether the speaker's identity fields belong here at
    all: Shared dossier and Classic card swap have already put them in the
    shared system body, so repeating them after history would bill the same
    text twice. Its post-history instructions are active-only in every mode and
    stay here regardless.

    *macros* is the turn's own resolver, scoped to the speaker through the one
    helper the cached body also uses (``group_context.member_macros``). It must be
    that object rather than a rebuilt one: the same card text has to resolve
    identically whether the mode routed it here or into the shared body, and the
    conversation seed it carries is what keeps a ``{{roll}}`` in a card from
    re-rolling every turn and busting this tail's bytes.

    *tools_sent* gates the no-tools nudge. It is the strongest provider-neutral
    signal Orb owns: a server may still narrow the supplied array based on
    ``tool_choice``. The lane resolves it from the frozen schema tuple and the
    call's actual transport shape.

    *depth_block* (``at_depth`` lorebook entries) goes last, *after* the user
    message — the ``@ Depth`` position, which is the whole point of the
    flag: the directives sit at the generation boundary.
    """
    tail = ""
    if speaker is not None:
        speaker_macros = member_macros(macros, speaker, macros.cast if macros else "")
        resolve = speaker_macros.resolve_message if speaker_macros else (lambda text: text)
        tail += f"## You are writing as {speaker.name}\n"
        if tail_carries_identity(context_mode):
            if speaker.private_sheet:
                tail += f"{SHEET_FRAMING}\n"
                tail += resolve(speaker.private_sheet) + "\n\n"
            if speaker.mes_example:
                example = resolve(speaker.mes_example)
                tail += (
                    example.replace("<START>", "## Example Dialogue")
                    if "<START>" in example
                    else f"## Example Dialogue\n{example}"
                )
                tail += "\n\n"
        if speaker.post_history and not prevent_prompt_overrides:
            tail += f"## Additional Instructions\n{resolve(speaker.post_history)}\n\n"
    if lorebook_block:
        tail += "___\n\n" + lorebook_block + "\n\n"
    if inj_block:
        tail += "___\n\n" + inj_block + "\n\n"
    if tools_sent:
        tail += "**Do not use tool or function calls this turn.**\n\n"
    tail += writer_nudge(length_guard)
    if speaker_beat:
        tail += f"## Your beat\n{speaker_beat}\n\n"
    if effective_msg:
        tail += "___\n\n" + effective_msg + "\n\n"
    if depth_block:
        tail += "___\n\n" + depth_block + "\n\n"
    if speaker is not None:
        tail += f"Write the next reply as {speaker.name} only. Do not write dialogue or actions for another cast member."

    return build_multimodal_content(tail, attachments)


async def writer_pass(
    client: LLMClient,
    base: CachedBase,
    settings: Mapping[str, Any],
    content: str | list[ContentPart],
    *,
    kv_tracker=None,
    reasoning_on: bool = True,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield ``{"type": "content"|"reasoning", "delta": str}`` dicts.

    *content* is the writer's user-message body, prebuilt by
    ``build_writer_content`` and shared with the editor. The tool blob comes from
    *base* so it stays byte-identical with the director and editor passes.
    """
    trailing: list[ChatMessage] = [{"role": "user", "content": content}]

    hyperparams = extract_hyperparams(settings)
    logger.info(
        "Writer pass: tools included=%s",
        json.dumps([t["function"]["name"] for t in base.tools]) if base.tools else "[]",
    )

    async for item in base.complete(
        client,
        label="writer",
        trailing=trailing,
        # base.tools is empty in dual-model (Invariant 5) → no tools, no
        # tool_choice; otherwise the writer ships the shared blob but is barred
        # from calling anything.
        tool_choice="none" if base.tools else None,
        kv_tracker=kv_tracker,
        **reasoning_cfg(reasoning_on, reasoning_prefill),
        **hyperparams,
    ):
        if item["type"] == "done":
            return
        yield item


async def writer_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: _KVCacheTracker,
    depth_block: str = "",
    speaker: CastMember | None = None,
    speaker_beat: str = "",
    macros: Macros | None = None,
    context_mode: GroupContextMode = "private",
) -> AsyncIterator[dict]:
    """Input-prep + writer pass + event translation.

    Builds ``state.writer_content`` once (replayed verbatim by the editor to
    extend the writer's KV-cached prefix), runs :func:`writer_pass` translating
    ``content``→``token`` and ``reasoning``→``reasoning`` events, and accumulates
    the writer's wall time into ``state.latency``.
    """
    # Probe only the message shape that chooses the transport. The final text
    # cannot affect that choice; images in the frozen history or current
    # attachments can. ModelLane then combines it with the actual frozen tools
    # tuple, so an all-false enablement map cannot masquerade as schemas.
    transport_probe: ChatMessage = {
        "role": "user",
        "content": build_multimodal_content("", attachments),
    }
    tools_sent = cfg.writer_lane.sends_tool_schemas([transport_probe])

    state.writer_content = build_writer_content(
        state.writer_lorebook_block,
        state.inj_block,
        tools_sent,
        state.effective_msg,
        attachments,
        cfg.length_guard,
        depth_block=depth_block,
        speaker=speaker,
        speaker_beat=speaker_beat,
        macros=macros,
        prevent_prompt_overrides=bool(settings.get("prevent_prompt_overrides")),
        context_mode=context_mode,
    )
    writer_t0 = time.monotonic()
    label_buffer = ""
    label_pending = speaker is not None
    # Long names are intentionally supported. Markdown wrappers, a heading,
    # punctuation and a newline fit inside this name-derived gate.
    label_buffer_bound = len(speaker.name) + 32 if speaker is not None else 0

    async for item in writer_pass(
        cfg.writer_lane.client,
        cfg.writer_lane.base,
        settings,
        state.writer_content,
        kv_tracker=kv_tracker,
        reasoning_on=cfg.writer_reasoning_on,
        reasoning_prefill=cfg.writer_reasoning_prefill,
    ):
        if item["type"] == "reasoning":
            state.reasoning_writer += item["delta"]
            yield {
                "event": "reasoning",
                "data": {"pass": "writer", "delta": item["delta"]},
            }
        else:
            delta = item["delta"]
            if label_pending and speaker is not None:
                label_buffer += delta
                stripped = strip_speaker_label(label_buffer, speaker.name)
                if stripped != label_buffer or "\n" in label_buffer or len(label_buffer) >= label_buffer_bound:
                    label_pending = False
                    label_buffer = ""
                    if stripped:
                        state.resp_text += stripped
                        yield {"event": "token", "data": stripped}
                continue
            state.resp_text += delta
            yield {"event": "token", "data": delta}
    if label_pending and speaker is not None:
        stripped = strip_speaker_label(label_buffer, speaker.name)
        if stripped:
            state.resp_text += stripped
            yield {"event": "token", "data": stripped}
    # agent_latency_ms is the whole turn's wall time; accumulate the writer's
    # span here (director + editor add their own).
    state.latency += int((time.monotonic() - writer_t0) * 1000)
