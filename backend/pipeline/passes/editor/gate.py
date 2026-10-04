"""Ask the Judge whether a post-processing fragment applies to the draft."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

import httpx

from ....core.llm_types import ParsedToolCall
from ....inference import (
    MAX_QUESTION_BYTES,
    MAX_STATE_BYTES,
    AbortToken,
    DecisionClient,
    DecisionQuestion,
    DecisionTransportError,
    LLMCallError,
)
from ..judge import JudgeConfig, SkipReason
from ..judge.judge import REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

GATE_THRESHOLD = 0.5
# Shared by every gate in one post-processing step; Editor calls do not spend it.
GATE_BUDGET_SECONDS = REQUEST_TIMEOUT_SECONDS
GATE_RECORD_NAME = "post_processing_gate"

# Validated against the configured Judge on fixed drafts: bare "Yes."/"No." put
# a borderline two-action reply exactly on the cutoff, where it fires.
GATE_CRITERIA: Mapping[str, str] = {
    "true": "The answer to the question is yes based on the reply.",
    "false": "The answer to the question is no based on the reply.",
}

CONDITION_MET = "condition_met"
CONDITION_NOT_MET = "condition_not_met"


def gate_question(fragment: Mapping[str, Any]) -> str:
    """The fragment's gate text, or ``""`` when it runs every turn."""
    value = fragment.get("post_processing_gate")
    return value.strip() if isinstance(value, str) else ""


def gate_replies(fragment: Mapping[str, Any]) -> int:
    """How many previous replies the fragment's gate shows the Judge."""
    value = fragment.get("post_processing_gate_replies")
    return value if isinstance(value, int) and value > 0 else 0


def gate_state(effective_msg: str, draft: str, previous_replies: Sequence[str] = ()) -> str:
    """The Judge's view: *previous_replies* (oldest first), the request, the draft."""
    context = "".join(f"Previous reply:\n{reply}\n\n" for reply in previous_replies)
    return f"{context}Current request:\n{effective_msg}\n\nReply:\n{draft}"


def _record(
    fragment: Mapping[str, Any], question: str, *, fired: bool, reason: str, previous_replies: int = 0, **extra: Any
) -> ParsedToolCall:
    arguments: dict[str, Any] = {
        "fragment_id": fragment.get("id", ""),
        "label": fragment.get("label") or fragment.get("id", ""),
        "question": question,
        "fired": int(fired),
        "reason": reason,
        **extra,
    }
    if previous_replies:
        arguments["previous_replies"] = previous_replies
    return {"name": GATE_RECORD_NAME, "arguments": arguments}


async def judge_gate(
    config: JudgeConfig | None,
    fragment: Mapping[str, Any],
    *,
    effective_msg: str,
    draft: str,
    timeout_seconds: float,
    recent_replies: Sequence[str] = (),
    abort: AbortToken | None = None,
) -> ParsedToolCall:
    """Evaluate *fragment*'s gate on *draft* and return its Inspector record.

    *recent_replies* is the conversation's assistant replies, newest first; the gate shows the Judge as many as the fragment
    asks for. Anything that keeps the Judge from answering fails open (``fired: 1``) and records why. A Stop raises
    ``DecisionCancelled`` instead.
    """
    question = gate_question(fragment)
    previous = list(reversed(recent_replies[: gate_replies(fragment)]))
    record = partial(_record, fragment, question, previous_replies=len(previous))
    if config is None or not config.configured:
        return record(fired=True, reason=SkipReason.NOT_CONFIGURED)

    state = gate_state(effective_msg, draft, previous)
    state_bytes = len(state.encode())
    question_bytes = len(question.encode()) + sum(len(text.encode()) for text in GATE_CRITERIA.values())
    if state_bytes > MAX_STATE_BYTES or question_bytes > MAX_QUESTION_BYTES:
        return record(
            fired=True,
            reason=SkipReason.OVERSIZED_INPUT,
            oversize_state_bytes=state_bytes,
            oversize_question_bytes=question_bytes,
            state_limit=MAX_STATE_BYTES,
            question_limit=MAX_QUESTION_BYTES,
        )

    if timeout_seconds <= 0:
        return record(fired=True, reason=SkipReason.BUDGET_EXHAUSTED)

    key = str(fragment.get("id", "") or "gate")
    timeout = min(GATE_BUDGET_SECONDS, timeout_seconds)
    client = DecisionClient(config.url, config.api_key, config.model, timeout=timeout, proxy=config.proxy)
    try:
        # httpx times each phase separately; this bounds the whole request.
        async with asyncio.timeout(timeout):
            response = await client.decide(
                state, [DecisionQuestion(key, question, GATE_CRITERIA)], timeout=timeout, abort=abort
            )
    except (httpx.TimeoutException, TimeoutError):
        return record(fired=True, reason=SkipReason.TIMEOUT)
    except (LLMCallError, DecisionTransportError, httpx.HTTPError) as exc:
        logger.warning("Post-processing gate for %r failed (%r); running the fragment", key, exc)
        return record(fired=True, reason=SkipReason.TRANSPORT_FAILURE)

    answer = response.answers.get(key)
    if not isinstance(answer, float):
        return record(fired=True, reason=SkipReason.INVALID_ANSWER)
    met = answer >= GATE_THRESHOLD
    return record(fired=met, reason=CONDITION_MET if met else CONDITION_NOT_MET, probability=answer)
