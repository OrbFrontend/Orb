"""
passes/editor/feedback.py — Feedback step of the editor pass.

A post-writer phase that produces an out-of-character note for the *user* (not
the writer). It runs at the end of the editor pass, reads the final (edited)
reply, and calls ``give_feedback`` with the enabled ``field_type='feedback'``
interactive fragments as its parameters.

This inverts the Interactive Fragment direction: where ``direct_scene`` steers
the writer (AI->AI), ``give_feedback`` surfaces a note to the player (AI->user).
Like ``direct_scene``, the ``give_feedback`` schema rides the shared per-turn
tools blob (registered in ``tool_defs.TOOLS``, built once by the orchestrator and
threaded to every pass via ``schema_overrides``). This step therefore reuses the
unchanged shared base and merely forces ``tool_choice=give_feedback``, so it adds
zero cache miss on the prefix+tools region — no blob swap, nothing to restore.

For the same reason it must not fork the *message* stack either: it replays the
writer's exact user message and the reply as a real assistant turn (mirroring the
editor in ``editor.py``) so the call extends the warm writer/editor prefix rather
than appending a single fresh message after ``base.prefix``. The latter collapsed
the provider cache to just the system+tools block — the message-side half of the
feedback cache bust, the counterpart to the tools-blob fix.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Sequence

from ...llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ...kv_tracker import CachedBase
from ...tool_defs import build_feedback_tool, GIVE_FEEDBACK_CHOICE
from ...prompt_builder import build_feedback_prompt
from ...llm_types import ChatMessage, ContentPart
from ...utils import extract_hyperparams

logger = logging.getLogger(__name__)


@dataclass
class FeedbackResult:
    """Typed payload of the feedback step's terminal ``done`` event.

    ``values`` is the ``give_feedback`` arguments, keyed by feedback-fragment id
    (empty/None entries dropped, mirroring the director's ``extra_fields``).
    ``agent_raw`` is the raw model response, kept for logging only.
    """

    values: dict = field(default_factory=dict)
    agent_raw: str = ""


def extract_feedback_values(tool_calls: list[dict]) -> dict:
    """Pull the ``give_feedback`` arguments out of parsed tool calls.

    Empty/None entries are dropped (mirroring the director's ``extra_fields``), so
    a model that omits or blanks a field contributes nothing. A later call wins on
    key collisions, matching ``apply_tool_calls``' update semantics. Each value is
    normally a string (``build_feedback_tool`` declares string params); the empty
    ``[]`` guard is defensive against a model that returns a list anyway, matching
    the frontend's array handling in ``chat_inspector.feedbackRows``.
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
    writer_user_msg: "str | list[ContentPart]",
    kv_tracker=None,
    reasoning_on: bool = False,
) -> AsyncIterator[dict]:
    """Yields reasoning dicts during the call, then a single done dict.

    Yields:
        {"type": "reasoning", "delta": str}          — zero or more reasoning chunks
        {"type": "done", "result": FeedbackResult}   — terminal step result

    *base* is the editor lane's cached base, which already carries give_feedback
    in its tools blob (the orchestrator registered the schema and threaded it via
    schema_overrides). We reuse it untouched and only force
    tool_choice=give_feedback, so this call hits the shared prefix+tools cache.

    The trailing replays the writer's exact user message and the reply as a real
    ``assistant`` turn — mirroring the editor's stack (passes/editor.py) — so this
    call *extends* the writer/editor KV-cached prefix instead of forking off the
    bare base.prefix with a fresh single message. Forking would collapse the hit
    to just the system+tools block (the post-fix feedback cache bust); extending
    reuses ``prefix + writer_user_msg + reply`` and leaves only the short feedback
    request as new bytes. *writer_user_msg* must be the same value threaded to the
    editor's ``writer_user_msg`` so both share the writer's cached prefix.
    """
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

    hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.4, "max_tokens": 2048})

    resp: dict = {}
    # Errors propagate out like the director/writer/editor passes.
    async for event in base.complete(
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
        ),
    }
