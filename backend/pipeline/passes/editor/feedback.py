"""Generate optional user-facing feedback after editing."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....core.llm_types import CompletionMessage
from ....inference import CachedBase, LLMClient, parse_tool_calls, reasoning_cfg
from ....prompting.tool_schemas import GIVE_FEEDBACK_CHOICE, build_feedback_tool
from .prompts import build_feedback_prompt

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FeedbackResult:
    """Typed result of the feedback step, yielded as the ``done`` event payload.

    ``values`` holds the ``give_feedback`` arguments keyed by fragment id; empty or None entries are dropped (mirroring the
    director's ``extra_fields``). ``agent_raw`` is the raw model response, kept for logging.
    """

    values: dict = field(default_factory=dict)
    agent_raw: str = ""


def extract_feedback_values(tool_calls: Sequence[Mapping[str, Any]]) -> dict:
    """Pull the ``give_feedback`` arguments from parsed tool calls.

    Empty or None entries are dropped. A later call wins on key collisions, matching ``apply_tool_calls`` semantics. Each value
    is normally a string; the empty ``[]`` guard is defensive against a model that returns a list, matching the frontend's array
    handling in ``message_inspector.buildFeedbackHtml``.
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
) -> AsyncIterator[Mapping[str, Any]]:
    """Yield call reasoning followed by one result event."""
    if not feedback_fragments:
        yield {"type": "done", "result": FeedbackResult()}
        return

    # The live view of the shared ``give_feedback``: the blob offers every defined feedback fragment with nothing required, so
    # an enable toggle never rewrites it. This one lists the enabled fragments for the prompt and narrows the call
    # (``json_schema``) where the transport can; the wire tools blob is the unchanged base.
    tool_schema = build_feedback_tool(feedback_fragments)

    request = build_feedback_prompt(feedback_fragments, reasoning_on=reasoning_on, tool_schema=tool_schema)
    # Replay writer_user_msg + reply (as the editor does) so the feedback call
    # continues the warm writer/editor stack; only `request` is new bytes.
    trailing: list[ChatMessage] = [
        {"role": "user", "content": writer_user_msg},
        {"role": "assistant", "content": reply_text},
        {"role": "user", "content": request},
    ]

    hyperparams = extract_hyperparams(settings, lane="agent")

    resp: CompletionMessage = {}
    # Errors propagate; editor_pass reports them as a non-terminal warning.
    async for event in base.complete_into(
        client,
        resp,
        label="feedback",
        trailing=trailing,
        tool_choice=GIVE_FEEDBACK_CHOICE,
        kv_tracker=kv_tracker,
        json_schema=tool_schema["function"]["parameters"],
        **hyperparams,
        **reasoning_cfg(reasoning_on, reasoning_prefill),
    ):
        yield event

    # A stop cuts the call short; its arguments are not a finished judgement.
    if client.is_aborted:
        yield {"type": "done", "result": FeedbackResult()}
        return

    agent_raw = json.dumps(resp, default=str)
    logger.info("Feedback step output:\n%s", agent_raw)

    live = {fragment["id"] for fragment in feedback_fragments}
    values = {key: value for key, value in extract_feedback_values(parse_tool_calls(resp)).items() if key in live}

    yield {"type": "done", "result": FeedbackResult(values=values, agent_raw=agent_raw)}
