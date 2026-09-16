"""Spark-TTS's audio vocabulary, as token ids rather than text.

Upstream drives this model through STRINGS: it renders speaker tokens as
``<|bicodec_global_3363|>…``, generates, then regexes ``bicodec_semantic_(\\d+)``
back out of the decoded completion. That cannot work against llama-server.
Every bicodec token is typed ``CONTROL`` in the GGUF and ``--special`` defaults
to false, so ``/completion`` returns ``content: ''`` and the regex finds
nothing — silently, which is the worst available failure.

Both families are contiguous, so we work in ids and do arithmetic instead:

===================  =====  ==============  ==================
family               count  index range     token-id range
===================  =====  ==============  ==================
``bicodec_global``    4096  0 – 4095        151665 – 155760
``bicodec_semantic``  8192  0 – 8191        155761 – 163952
===================  =====  ==============  ==================

Everything in this module is pure: no model, no HTTP, no numpy.
``tests/unit/test_spark_tokens.py`` pins the exact id sequence for a known
``(text, speaker_tokens)`` pair, which is the only way a silent off-by-one in
a base constant gets caught before it reaches a listener as a different voice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Verified against models/Spark-TTS-0.5B/LLM/tokenizer.json.
GLOBAL_BASE = 151665
GLOBAL_COUNT = 4096
SEMANTIC_BASE = 155761
SEMANTIC_COUNT = 8192

GLOBAL_LAST = GLOBAL_BASE + GLOBAL_COUNT - 1  # 155760
SEMANTIC_LAST = SEMANTIC_BASE + SEMANTIC_COUNT - 1  # 163952

TASK_TTS = 165137
START_CONTENT = 165146
END_CONTENT = 165152
START_GLOBAL = 165150
END_GLOBAL = 165156
START_SEMANTIC = 165151

#: How many speaker tokens one enrollment produces. A property of the FSQ
#: quantizer (``token_num: 32`` in BiCodec's config), not a choice.
SPEAKER_TOKEN_COUNT = 32

#: Budget for one generation. Upstream passes a flat 3000 — about a minute of
#: audio — so a degenerate generation runs to that cap and costs ~2.5 minutes
#: on CPU for a line that needs eleven seconds. Semantic tokens come out at
#: 50 Hz and speech runs ~15 chars/second, so a line needs roughly 3.3 tokens
#: per character; 8 leaves double the headroom the slowest delivery asks for
#: while cutting the runaway case to a fraction of its cost. Carried over from
#: OrbTTS, where it was measured.
TOKENS_PER_CHAR = 8
TOKEN_FLOOR = 256
TOKEN_CEILING = 3000


class InvalidSpeakerTokens(ValueError):
    """A stored voice that is not 32 in-range ints.

    Raised at the boundary rather than passed to the codec, which answers a
    wrong-length array with an exception from deep inside an einsum.
    """


def token_budget(text: str) -> int:
    """Cap generation at what this line could plausibly need."""
    return min(TOKEN_CEILING, TOKEN_FLOOR + TOKENS_PER_CHAR * len(text))


def validate_speaker_tokens(raw: object) -> list[int]:
    """Coerce a stored value to 32 speaker indices, or explain what it was.

    Reached from the profile normaliser, from the synthesis path, and from the
    enrollment route, because "the thing in the database is the right shape" is
    not an assumption any of the three may make on its own.
    """
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
    """The cloning prompt, as token ids.

    ``text_tokens`` is the line tokenised with ``parse_special`` OFF — a
    character name that happens to spell ``<|end_content|>`` is text, not a
    control token, and asking the tokenizer to honour specials inside user text
    is how that stops being true.

    Note what is absent: no reference audio, no semantic tokens, no transcript.
    The 32 ints carry the timbre; the model writes the prosody.
    """
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
    """Codebook indices from generated token ids, in order.

    Anything outside the semantic range is dropped rather than trusted: on the
    cloning path the model should emit semantic tokens and nothing else, and an
    EOS or a stray text token reaching the codec as an index is a crash at best.
    """
    return [token - SEMANTIC_BASE for token in generated if SEMANTIC_BASE <= token <= SEMANTIC_LAST]


__all__ = [
    "END_CONTENT",
    "END_GLOBAL",
    "GLOBAL_BASE",
    "GLOBAL_COUNT",
    "GLOBAL_LAST",
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
