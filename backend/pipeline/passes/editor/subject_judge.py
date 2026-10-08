"""Ask the Judge whether the draft repeats a subject's description from each recent reply."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

import httpx

from ....analysis.subjects import SUBJECT_DESCRIPTIONS, subjects_input
from ....core.llm_types import ParsedToolCall
from ....inference import (
    MAX_QUESTIONS_PER_REQUEST,
    MAX_STATE_BYTES,
    AbortToken,
    DecisionClient,
    DecisionQuestion,
    DecisionTransportError,
    LLMCallError,
)
from ..judge import JudgeConfig
from ..judge.judge import REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

RECORD_NAME = "subject_fixation_judge"

# Two readings of one pair, averaged.
_READINGS = (
    (
        "same",
        "The NEW REPLY describes {subject} using a detail already given in the EARLIER REPLY, such as the same color, "
        "shape, texture, sound or other sensory quality. The detail describes the same character, object or scene feature "
        "in both replies, even if worded differently. A mere mention or an action without descriptive detail does not count.",
    ),
    (
        "repeat",
        "The NEW REPLY repeats a descriptive detail about {subject} from the EARLIER REPLY. Both replies describe the same "
        "character, object or scene feature with the same physical or sensory quality, including paraphrases. A new detail, "
        "a mere mention or an action without descriptive detail does not count.",
    ),
)
MAX_SUBJECTS = MAX_QUESTIONS_PER_REQUEST // len(_READINGS)


def pair_state(earlier: str, draft: str) -> str:
    return f"EARLIER REPLY (narration only):\n{earlier}\n\nNEW REPLY (narration only):\n{draft}"


def pair_questions(categories: Sequence[str]) -> list[DecisionQuestion]:
    return [
        DecisionQuestion(f"{category}.{key}", text.format(subject=SUBJECT_DESCRIPTIONS[category]), {})
        for category in categories
        for key, text in _READINGS
    ]


def _scores(answers: Mapping[str, object], categories: Sequence[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for category in categories:
        values = [v for key, _ in _READINGS if isinstance(v := answers.get(f"{category}.{key}"), float)]
        out[category] = sum(values) / len(values) if len(values) == len(_READINGS) else None
    return out


async def repeat_scores(
    config: JudgeConfig,
    draft: str,
    earlier: Sequence[str],
    categories: Sequence[str],
    *,
    abort: AbortToken | None = None,
) -> tuple[dict[str, list[float | None]], ParsedToolCall]:
    """Per category, the Judge's repeat probability against each of *earlier* (newest first) and the Inspector record.

    One request per earlier reply, all at once. A reply the Judge could not read (too long, timed out, failed) scores ``None``,
    which never counts as a repeat. A Stop raises ``DecisionCancelled``.
    """
    categories = list(categories)[:MAX_SUBJECTS]
    draft_narration = subjects_input(draft)
    questions = pair_questions(categories)
    client = DecisionClient(config.url, config.api_key, config.model, timeout=REQUEST_TIMEOUT_SECONDS, proxy=config.proxy)

    async def read(text: str) -> dict[str, float | None]:
        state = pair_state(subjects_input(text), draft_narration)
        if len(state.encode()) > MAX_STATE_BYTES:
            return dict.fromkeys(categories)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await client.decide(state, questions, abort=abort)
        except (httpx.TimeoutException, TimeoutError, LLMCallError, DecisionTransportError, httpx.HTTPError) as exc:
            logger.warning("Subject fixation Judge read failed (%r); that reply counts as no repeat", exc)
            return dict.fromkeys(categories)
        return _scores(response.answers, categories)

    reads = await asyncio.gather(*(read(text) for text in earlier))
    scores = {category: [r[category] for r in reads] for category in categories}
    record: ParsedToolCall = {
        "name": RECORD_NAME,
        "arguments": {category: [None if s is None else round(s, 3) for s in row] for category, row in scores.items()},
    }
    return scores, record
