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

#: The GGUF's end-of-generation tokens, ``<|endoftext|>`` and ``<|im_end|>``.
STOP_TOKENS = (151643, 151645)

#: Number of speaker tokens produced by enrollment.
SPEAKER_TOKEN_COUNT = 32

#: Semantic tokens per second of reference audio (one per 320-sample hop).
SEMANTIC_RATE = 50

#: Longest reference excerpt, in seconds and in the tokens it becomes.
MAX_REFERENCE_SECONDS = 12
MAX_REFERENCE_TOKENS = SEMANTIC_RATE * MAX_REFERENCE_SECONDS

#: Longest reference transcript. Twelve seconds of speech is well under this
#: in any language; a longer transcript cannot be the excerpt's.
MAX_REFERENCE_TEXT = 400

#: Maximum semantic tokens for one line.
TOKENS_PER_CHAR = 8
TOKEN_FLOOR = 256
TOKEN_CEILING = 3000


class InvalidSpeakerTokens(ValueError):
    """A stored voice is not 32 in-range integers."""


class InvalidReferenceTokens(ValueError):
    """A stored reference excerpt is not a run of in-range semantic indices."""


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


def validate_reference_tokens(raw: object) -> list[int]:
    """Validate and return a reference excerpt's semantic indices."""
    if not isinstance(raw, (list, tuple)):
        raise InvalidReferenceTokens("reference tokens must be a list of integers")
    if not 0 < len(raw) <= MAX_REFERENCE_TOKENS:
        raise InvalidReferenceTokens(f"reference tokens must number 1 to {MAX_REFERENCE_TOKENS}, got {len(raw)}")
    out: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidReferenceTokens("reference tokens must all be integers")
        if not 0 <= value < SEMANTIC_COUNT:
            raise InvalidReferenceTokens(f"reference token {value} is outside [0, {SEMANTIC_COUNT})")
        out.append(value)
    return out


def _is_cjk(char: str) -> bool:
    # CJK punctuation, kana, ideographs and fullwidth forms: scripts written
    # without spaces between words. Hangul is spaced and stays out.
    return "\u2e80" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" or "\uff00" <= char <= "\uffef"


def join_content(reference_text: str, text: str) -> str:
    """The reference transcript followed by the line to speak.

    Upstream concatenates the two with nothing between them, which reads
    correctly for Chinese and runs two English sentences together, so a space
    goes between them unless either side of the seam is a CJK character.
    """
    head, tail = reference_text.strip(), text.strip()
    if not head or not tail:
        return head or tail
    return head + ("" if _is_cjk(head[-1]) or _is_cjk(tail[0]) else " ") + tail


def clone_prompt(
    text_tokens: Sequence[int],
    speaker_tokens: Sequence[int],
    reference_tokens: Sequence[int] = (),
) -> list[int]:
    """Build the cloning prompt from text, speaker and reference token ids.

    With *reference_tokens*, the prompt continues past the speaker block into
    the reference excerpt's own speech, and *text_tokens* must spell the
    excerpt's transcript followed by the line to speak (see
    :func:`join_content`). The model then continues that speech, and what it
    generates is the new line alone.
    """
    speaker = validate_speaker_tokens(list(speaker_tokens))
    prompt = [
        TASK_TTS,
        START_CONTENT,
        *text_tokens,
        END_CONTENT,
        START_GLOBAL,
        *(GLOBAL_BASE + index for index in speaker),
        END_GLOBAL,
    ]
    if reference_tokens:
        reference = validate_reference_tokens(list(reference_tokens))
        prompt += [START_SEMANTIC, *(SEMANTIC_BASE + index for index in reference)]
    return prompt


def semantic_indices(generated: Iterable[int]) -> list[int]:
    """Extract in-range semantic codebook indices from generated ids."""
    return [token - SEMANTIC_BASE for token in generated if SEMANTIC_BASE <= token <= SEMANTIC_LAST]


__all__ = [
    "END_CONTENT",
    "END_GLOBAL",
    "GLOBAL_BASE",
    "GLOBAL_COUNT",
    "InvalidReferenceTokens",
    "InvalidSpeakerTokens",
    "MAX_REFERENCE_SECONDS",
    "MAX_REFERENCE_TEXT",
    "MAX_REFERENCE_TOKENS",
    "SEMANTIC_BASE",
    "SEMANTIC_COUNT",
    "SEMANTIC_LAST",
    "SEMANTIC_RATE",
    "SPEAKER_TOKEN_COUNT",
    "START_CONTENT",
    "START_GLOBAL",
    "START_SEMANTIC",
    "STOP_TOKENS",
    "TASK_TTS",
    "TOKEN_CEILING",
    "clone_prompt",
    "join_content",
    "semantic_indices",
    "token_budget",
    "validate_reference_tokens",
    "validate_speaker_tokens",
]
