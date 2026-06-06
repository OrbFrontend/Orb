"""
passes/editor/feedback.py — Feedback step of the editor pass.

A post-writer phase that produces an out-of-character note for the *user* (not
the writer). It runs at the end of the editor pass, reads the final (edited)
reply, and calls ``give_feedback`` with the enabled ``field_type='feedback'``
interactive fragments as its parameters.

This inverts the Interactive Fragment direction: where ``direct_scene`` steers
the writer (AI->AI), ``give_feedback`` surfaces a note to the player (AI->user).
The give_feedback tool is carried only here — never in the shared
director/writer/editor tools blob. It is swapped onto a copy of the editor base
(via :func:`dataclasses.replace`), so this single call is a deliberate cache
miss; the shared base is left intact for the next turn (see TOFIX: "cache busts
on feedback").
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Mapping, Sequence

from ...llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ...kv_tracker import CachedBase
from ...tool_defs import build_feedback_tool, GIVE_FEEDBACK_CHOICE
from ...prompt_builder import build_feedback_prompt
from ...llm_types import ChatMessage
from ...utils import extract_hyperparams

logger = logging.getLogger(__name__)


@dataclass
class FeedbackResult:
    """Typed payload of the feedback step's terminal ``done`` event.

    ``values`` is the ``give_feedback`` arguments, keyed by feedback-fragment id
    (empty/None entries dropped, mirroring the director's ``extra_fields``).
    """

    values: dict = field(default_factory=dict)
    agent_raw: str = ""
    latency: int = 0


def extract_feedback_values(tool_calls: list[dict]) -> dict:
    """Pull the ``give_feedback`` arguments out of parsed tool calls.

    Empty/None entries are dropped (mirroring the director's ``extra_fields``), so
    a model that omits or blanks a field contributes nothing. A later call wins on
    key collisions, matching ``apply_tool_calls``' update semantics.
    """
    values: dict = {}
    for tc in tool_calls:
        if tc.get("name") == "give_feedback":
            args = tc.get("arguments", {})
            values.update({k: v for k, v in args.items() if v not in (None, "", [])})
    return values


async def feedback_step(
    client: LLMClient,
    base: CachedBase,
    reply_text: str,
    settings: Mapping[str, Any],
    feedback_fragments: Sequence[Mapping[str, Any]],
    *,
    kv_tracker=None,
    reasoning_on: bool = False,
) -> AsyncIterator[dict]:
    """Yields reasoning dicts during the call, then a single done dict.

    Yields:
        {"type": "reasoning", "delta": str}          — zero or more reasoning chunks
        {"type": "done", "result": FeedbackResult}   — terminal step result

    *base* is the editor lane's cached base; the give_feedback tool replaces its
    tools blob (via :func:`dataclasses.replace`), so this one call is a cache miss
    while the shared *base* object stays untouched for subsequent turns.
    """
    if not feedback_fragments:
        yield {"type": "done", "result": FeedbackResult()}
        return

    tool_schema = build_feedback_tool(feedback_fragments)
    feedback_base = replace(base, tools=(tool_schema,))

    tail = build_feedback_prompt(
        reply_text,
        feedback_fragments,
        reasoning_on=reasoning_on,
        tool_schema=tool_schema,
    )
    trailing: list[ChatMessage] = [{"role": "user", "content": tail}]

    hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.4, "max_tokens": 2048})

    t0 = time.monotonic()
    resp: dict = {}
    # Errors propagate out like the director/writer/editor passes.
    async for event in feedback_base.complete(
        client,
        label="feedback",
        trailing=trailing,
        tool_choice=GIVE_FEEDBACK_CHOICE,
        kv_tracker=kv_tracker,
        **hyperparams,
        **reasoning_cfg(reasoning_on),
    ):
        if event["type"] == "reasoning":
            yield {"type": "reasoning", "delta": event["delta"]}
        elif event["type"] == "done":
            resp = event["message"]

    agent_raw = json.dumps(resp, default=str)
    logger.info("Feedback step output:\n%s", agent_raw)

    values = extract_feedback_values(parse_tool_calls(resp))

    yield {
        "type": "done",
        "result": FeedbackResult(
            values=values,
            agent_raw=agent_raw,
            latency=int((time.monotonic() - t0) * 1000),
        ),
    }
