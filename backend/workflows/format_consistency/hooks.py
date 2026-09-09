"""Bind format and voice normalization to the post-pipeline hook."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    EV_DRAFT_REPLACED,
    AxisStyle,
    baseline_axes,
    forced_tool_call,
    get_workflow_config,
    normalize_to_baseline,
)
from . import (
    VOICE_REWRITE_LENGTH_RULE,
    VOICE_REWRITE_TOOL_NAME,
    WORKFLOW_ID,
    normalize_config,
)
from .voice import classifier_ready, classify, drift, labels_for, target

logger = logging.getLogger(__name__)

BASELINE_WINDOW = 3

# The rewrite runs on its own lane rather than extending the turn's prompt: a
# voice restatement is a closed transform over the draft, so the scene, the cast
# and the history buy it nothing and cost a full conversation's prompt tokens on
# every drifting turn. Servers keep per-sequence caches (llama.cpp parks idle
# slots in its host-RAM prompt cache; vLLM hashes blocks), so a second short lane
# sits alongside the conversation's rather than displacing it.
#
# Constant, so it is the whole cached prefix of that lane -- warmed once and
# reused by every rewrite in every conversation. Everything per-call rides the
# user message behind it. What the rewrite must not touch is stated here because
# the editor may have just rewritten this draft for the length guard: a voice fix
# that grows the reply would silently undo that pass.
_SYSTEM = (
    "You are a copy editor. You restate a passage of prose in a different narrative "
    "voice and change nothing else.\n\n"
    "RULES:\n"
    "- Change ONLY the narrative voice. Keep every story beat, every line of "
    "dialogue, the author's vocabulary, and all formatting exactly as they are.\n"
    f"- {VOICE_REWRITE_LENGTH_RULE}\n"
    "- Restate the whole passage, not an excerpt.\n"
    "- Pronouns and names already in the passage keep their referents. Do not "
    "introduce a character, a name, or a detail the passage does not contain."
)

_INSTRUCTION = (
    f"Restate the passage below in {{voice}}. Call `{VOICE_REWRITE_TOOL_NAME}` with the result.\n\nPASSAGE:\n{{draft}}"
)


def _baseline_window(history) -> list[Mapping[str, Any]]:
    """The recent assistant-message rows (newest first, up to 3) whose conventions
    the draft is held to.

    Mirrors the fallback window the editor pass derived from its cached prefix:
    assistant history is always plain text, so a non-str body (the multimodal
    list form rides only user messages) has nothing to contribute. Rows rather
    than bare content because the voice half caches its labels per message id.
    """
    window: list[Mapping[str, Any]] = []
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                window.append(msg)
                if len(window) >= BASELINE_WINDOW:
                    break
    return window


async def _voice_enabled(ctx) -> bool:
    """Whether the voice half runs this turn.

    The free precondition is asked first: ``classifier_ready`` reads the settings
    mapping the ctx already carries, so a host without the POV/tense model never
    pays the config slot's round trip.
    """
    if not classifier_ready(ctx.settings):
        return False
    return normalize_config(await get_workflow_config(WORKFLOW_ID))["voice_consistency"]


async def _voice_rewrite(ctx, text: str, phrases: list[str]) -> str:
    """Restate *text* in the baseline voice on a self-contained lane; "" on failure.

    Two messages, no conversation: a constant system prefix and one user message
    carrying the target voice and the draft. Restating a passage is closed over
    that passage, so the scene, the cast and the history are not inputs to it --
    and on a metered endpoint they are the whole bill. This call went from the
    turn's full prompt to a few hundred tokens by dropping what it never read.

    The lane is its own, not the turn's, which is the point: ``prefix`` here is a
    constant rather than the conversation prefix, and ``enabled_tools=None`` ships
    ``[voice_rewrite]`` alone instead of the turn's blob. Nothing about this call
    has to match what the Director and Writer sent, so nothing about it can
    diverge from them either -- the failure mode a shared lane invites, where a
    forced tool the turn's blob never declared appends a schema and evicts the
    conversation from the server's prefix cache.

    It still runs on the Agent model: it is a forced tool call, and in dual-model
    mode the writer lane is the one without schemas.
    """
    args: dict = {}
    async for event in forced_tool_call(
        client=ctx.agent_client or ctx.client,
        prefix=[{"role": "system", "content": _SYSTEM}],
        tail_messages=[
            {
                "role": "user",
                "content": _INSTRUCTION.format(voice=" and ".join(phrases), draft=text),
            }
        ],
        tool_name=VOICE_REWRITE_TOOL_NAME,
        settings=ctx.settings,
        model_name=ctx.agent_model_name or None,
        # None, not ctx.enabled_tools: forced_tool_call reads that as "ship the
        # forced tool alone", which is the whole array this lane wants.
        enabled_tools=None,
        kv_tracker=ctx.kv_tracker,
        cache_shape="format_consistency:voice_rewrite",
        reasoning_on=False,
        temperature=0.25,
        token_floor=8192,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    rewritten = args.get("rewritten_text")
    return rewritten if isinstance(rewritten, str) else ""


async def _hold_voice(
    ctx,
    text: str,
    window: list[Mapping[str, Any]],
    convention: AxisStyle,
) -> str:
    """The draft restated in the window's voice, or *text* unchanged.

    An ambiguous end, an empty rewrite, or a classifier that answered the sentinel
    all return *text*; anything that raises is caught by the caller.
    """
    baseline = target([await labels_for(msg, convention) for msg in window])
    if baseline == ("ambiguous", "ambiguous"):
        return text
    source = await classify(text, convention)
    # An empty or all-dialogue draft shapes to "" and classifies ambiguous, so it
    # falls out here by construction rather than by a length check.
    phrases = drift(source, baseline)
    if not phrases:
        return text
    logger.info(
        "format-consistency: voice drift %s -> %s; requesting a rewrite in %s",
        source,
        baseline,
        ", ".join(phrases),
    )
    rewritten = await _voice_rewrite(ctx, text, phrases)
    return rewritten if rewritten and rewritten != text else text


async def post_pipeline(ctx):
    """Hold the finished draft's markup convention -- and optionally its narrative
    voice -- to the recent messages'.

    Suspension is the framework's job: the per-workflow toggle gates this hook in
    the fan-out loop, so when reached the hook always runs. The two repairs
    compose before yielding, because the hook has a one-``draft_replaced`` budget
    (the bridge warns and drops a second).
    """
    window = _baseline_window(ctx.history)
    baseline_msgs = [msg.get("content", "") for msg in window]
    convention = baseline_axes(baseline_msgs)
    text = ctx.draft

    # One guard over the whole voice half. Markup normalization is this workflow's
    # always-on job; the voice check is opt-in and reaches a local model, the
    # config slot, the message-state cache and an LLM endpoint -- four things that
    # can fail independently. Any of them costs the check, never the markup fix.
    # Nothing in here raises WorkflowUserFacingError: that is for a
    # misconfiguration the user chose, not for a flaky call.
    try:
        if await _voice_enabled(ctx):
            text = await _hold_voice(ctx, text, window, convention)
    except Exception:
        logger.exception("format-consistency: voice check failed; normalizing markup only")
        text = ctx.draft

    # Always last: an LLM rewrite reintroduces markup drift, and this algorithmic
    # pass is the cheap authority on that.
    # The pure normalizer keeps an on/off param for its own test surface; the real
    # gate is the framework toggle, so this path always passes True.
    text, report = normalize_to_baseline(text, baseline_msgs, enabled=True, target=convention)
    if report.changed:
        logger.info("format-consistency: normalized draft (%s)", report.transition())
    if text != ctx.draft:
        yield {"type": EV_DRAFT_REPLACED, "draft": text}
