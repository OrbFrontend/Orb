"""Ask the Judge whether the draft repeats a subject's description from each recent reply."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

import httpx

from ....analysis.subjects import subjects_input
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

SUBJECTS: Mapping[str, str] = {
    "eyes": "eyes or gaze (color, shape, look of the eyes)",
    "hair": "hair on the head (not body or pubic hair)",
    "face": "face, expression, cheeks, blush, brows or jaw (not eyes or lips)",
    "mouth": "lips, mouth, smile, smirk, teeth or tongue of the face (not genital lips)",
    "voice": "a character's speaking voice: how it sounds (not the words said)",
    "breath": "breathing, heartbeat or pulse",
    "scent": "a scent or smell",
    "hands": "hands, fingers or nails",
    "skin": "skin itself: complexion, tone, texture, freckles, scars, flush across the skin",
    "chest": "chest or breasts",
    "lower_body": "hips, thighs, legs or backside",
    "neck": "neck, throat or shoulders",
    "build": "the whole body's size, height, physique or figure (not one part: breasts are chest, hips and backside are lower_body)",
    "clothing": "clothes a character wears",
    "accessory": "jewelry or accessories a character wears",
    "object": "a weapon or item a character holds or carries (not clothes, jewelry, furniture or the room)",
    "nonhuman": "non-human features: a halo, horns, wings, a tail, animal ears, scales",
    "light": "the light, glow or shadows of the scene",
    "sound": "the sounds of the scene: echoes, noises, background (not what is said)",
    "weather": "weather, air or temperature of the scene",
}

# Two readings of one pair, averaged.
_READINGS = (
    (
        "same",
        "The NEW REPLY describes {subject} with the same detail the EARLIER REPLY gave it: the same color, size, shape, "
        "texture, quality or image, even in other words.",
    ),
    (
        "repeat",
        "The NEW REPLY repeats a description of {subject} from the EARLIER REPLY: a reader would notice the same look, sound "
        "or image of it being described again. Only naming it, or having it act or move, is not a description.",
    ),
)
MAX_SUBJECTS = MAX_QUESTIONS_PER_REQUEST // len(_READINGS)


def pair_state(earlier: str, draft: str) -> str:
    return f"EARLIER REPLY:\n{earlier}\n\nNEW REPLY:\n{draft}"


def pair_questions(categories: Sequence[str]) -> list[DecisionQuestion]:
    return [
        DecisionQuestion(f"{category}.{key}", text.format(subject=SUBJECTS[category]), {})
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
