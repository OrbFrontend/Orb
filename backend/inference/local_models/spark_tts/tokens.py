"""Map Spark-TTS control and semantic tokens to integer ids."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Verified against models/Spark-TTS-0.5B/LLM/tokenizer.json.
GLOBAL_BASE = 151665
GLOBAL_COUNT = 4096
SEMANTIC_BASE = 155761
SEMANTIC_COUNT = 8192

SEMANTIC_LAST = SEMANTIC_BASE + SEMANTIC_COUNT - 1  # 163952

TASK_TTS = 165137
START_CONTENT = 165146
END_CONTENT = 165152
START_GLOBAL = 165150
END_GLOBAL = 165156
START_SEMANTIC = 165151

#: Number of speaker tokens produced by enrollment.
SPEAKER_TOKEN_COUNT = 32

#: Maximum semantic tokens for one line.
TOKENS_PER_CHAR = 8
TOKEN_FLOOR = 256
TOKEN_CEILING = 3000


class InvalidSpeakerTokens(ValueError):
    """A stored voice is not 32 in-range integers."""


def token_budget(text: str) -> int:
    """Cap generation at what this line could plausibly need."""
    return min(TOKEN_CEILING, TOKEN_FLOOR + TOKENS_PER_CHAR * len(text))


def validate_speaker_tokens(raw: object) -> list[int]:
    """Validate and return 32 speaker indices."""
    if not isinstance(raw, (list, tuple)):
        raise InvalidSpeakerTokens(f"speaker tokens must be a list of {SPEAKER_TOKEN_COUNT} integers")
    if len(raw) != SPEAKER_TOKEN_COUNT:
        raise InvalidSpeakerTokens(f"speaker tokens must be exactly {SPEAKER_TOKEN_COUNT} integers, got {len(raw)}")
    out: list[int] = []
    for value in raw:
        # bool is an int in Python and would index the codebook at 0 or 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidSpeakerTokens("speaker tokens must all be integers")
        if not 0 <= value < GLOBAL_COUNT:
            raise InvalidSpeakerTokens(f"speaker token {value} is outside [0, {GLOBAL_COUNT})")
        out.append(value)
    return out


def clone_prompt(text_tokens: Sequence[int], speaker_tokens: Sequence[int]) -> list[int]:
    """Build the cloning prompt from text and speaker token ids."""
    speaker = validate_speaker_tokens(list(speaker_tokens))
    return [
        TASK_TTS,
        START_CONTENT,
        *text_tokens,
        END_CONTENT,
        START_GLOBAL,
        *(GLOBAL_BASE + index for index in speaker),
        END_GLOBAL,
    ]


def semantic_indices(generated: Iterable[int]) -> list[int]:
    """Extract in-range semantic codebook indices from generated ids."""
    return [token - SEMANTIC_BASE for token in generated if SEMANTIC_BASE <= token <= SEMANTIC_LAST]


__all__ = [
    "END_CONTENT",
    "END_GLOBAL",
    "GLOBAL_BASE",
    "GLOBAL_COUNT",
    "InvalidSpeakerTokens",
    "SEMANTIC_BASE",
    "SEMANTIC_COUNT",
    "SEMANTIC_LAST",
    "SPEAKER_TOKEN_COUNT",
    "START_CONTENT",
    "START_GLOBAL",
    "START_SEMANTIC",
    "TASK_TTS",
    "TOKEN_CEILING",
    "clone_prompt",
    "semantic_indices",
    "token_budget",
    "validate_speaker_tokens",
]
