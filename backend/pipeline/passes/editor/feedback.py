"""Generate optional user-facing feedback after editing."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....inference import (
    CachedBase,
    LLMClient,
    parse_tool_calls,
    reasoning_cfg,
)
from ....prompting.tool_schemas import GIVE_FEEDBACK_CHOICE, build_feedback_tool
from .prompts import build_feedback_prompt

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FeedbackResult:
    """Typed result of the feedback step, yielded as the ``done`` event payload.

    ``values`` holds the ``give_feedback`` arguments keyed by fragment id; empty
    or None entries are dropped (mirroring the director's ``extra_fields``).
    ``agent_raw`` is the raw model response, kept for logging.
    """

    values: dict = field(default_factory=dict)
    agent_raw: str = ""


def extract_feedback_values(tool_calls: list[dict]) -> dict:
    """Pull the ``give_feedback`` arguments from parsed tool calls.

    Empty or None entries are dropped. A later call wins on key collisions,
    matching ``apply_tool_calls`` semantics. Each value is normally a string;
    the empty ``[]`` guard is defensive against a model that returns a list,
    matching the frontend's array handling in ``chat_inspector.feedbackRows``.
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
    writer_user_msg: str | list[ContentPart],
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield call reasoning followed by one result event."""
    if not feedback_fragments:
        yield {"type": "done", "result": FeedbackResult()}
        return

    # Built locally only to echo the parameter order into the prompt; it is
    # byte-identical to the override the orchestrator already put in the shared
    # base (same deterministic builder, same fragment list), so the wire tools
    # blob is the unchanged base — nothing diverges.
    tool_schema = build_feedback_tool(feedback_fragments)

    request = build_feedback_prompt(
        feedback_fragments,
        reasoning_on=reasoning_on,
        tool_schema=tool_schema,
    )
    # Replay writer_user_msg + reply (as the editor does) so the feedback call
    # continues the warm writer/editor stack; only `request` is new bytes.
    trailing: list[ChatMessage] = [
        {"role": "user", "content": writer_user_msg},
        {"role": "assistant", "content": reply_text},
        {"role": "user", "content": request},
    ]

    hyperparams = extract_hyperparams(settings, lane="agent", token_floor=2048, defaults={"temperature": 0.4})

    resp: dict = {}
    # Errors propagate out like the director/writer/editor passes.
    async for event in base.complete_into(
        client,
        resp,
        label="feedback",
        trailing=trailing,
        tool_choice=GIVE_FEEDBACK_CHOICE,
        kv_tracker=kv_tracker,
        **hyperparams,
        **reasoning_cfg(reasoning_on, reasoning_prefill),
    ):
        yield event

    agent_raw = json.dumps(resp, default=str)
    logger.info("Feedback step output:\n%s", agent_raw)

    values = extract_feedback_values(parse_tool_calls(resp))

    yield {
        "type": "done",
        "result": FeedbackResult(
            values=values,
            agent_raw=agent_raw,
        ),
    }
