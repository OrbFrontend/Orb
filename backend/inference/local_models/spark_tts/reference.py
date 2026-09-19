"""Choose and encode the reference excerpt for transcript-conditioned cloning.

Advanced cloning puts a few seconds of the reference speaker's own speech into
the prompt, as semantic tokens, together with what those seconds say. The
excerpt has to be short — every one of its tokens is re-read for every spoken
line — and has to start and end between words, or its transcript cannot match
it. So it is cut at pauses, from the stretch of the clip with the most speech,
and its own long pauses are shortened the way enrollment shortens them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import onnx_runtime
from . import catalog, silence
from .tokens import SEMANTIC_RATE, validate_reference_tokens

if TYPE_CHECKING:
    import numpy as np

SAMPLE_RATE = silence.SAMPLE_RATE
FRAME = silence.FRAME_SAMPLES

#: Longest excerpt, after its pauses are shortened.
TARGET_SECONDS = 10.0
#: Less speech than this carries too little delivery to be worth the prompt.
MIN_SPEECH_SECONDS = 2.0
#: A silence at least this long separates two phrases; shorter ones are
#: gaps inside a phrase and never become a cut.
MIN_PAUSE_MS = 200
#: A silence at least this long ends a sentence or a clause. The excerpt starts
#: and ends at one when the clip has one, so it does not open mid-thought.
BREAK_PAUSE_MS = 400
#: An excerpt that opens and closes at breaks is chosen over one that does not
#: while it holds at least this share of that one's speech; a clip of long
#: sentences would otherwise trade ten seconds of delivery for two.
BREAK_PREFERENCE = 0.5
#: Longest pause kept inside the excerpt. The same rule enrollment applies: a
#: dramatic two-second silence in the reference is copied as a speaking habit,
#: and generation trails off into silence instead of ending.
KEEP_PAUSE_MS = 200
#: Silence kept on each side of the excerpt, so its first and last sounds are
#: not clipped by a frame boundary.
EDGE_MS = 100


class NoUsableExcerpt(ValueError):
    """The clip has no stretch of speech long enough to act as a reference."""


@dataclass(frozen=True)
class Reference:
    """A reference excerpt as a prompt uses it: its speech and what it says."""

    text: str
    tokens: Sequence[int]


def _frames(ms: float) -> int:
    return max(1, round(ms * SAMPLE_RATE / 1000 / FRAME))


def _phrases(mask: np.ndarray) -> list[tuple[int, int]]:
    """``[start, end)`` frame runs of speech, bridging pauses shorter than a phrase break."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    spoken = np.flatnonzero(mask)
    if spoken.size == 0:
        return []
    phrases: list[tuple[int, int]] = []
    start = previous = int(spoken[0])
    for frame in spoken[1:]:
        frame = int(frame)
        if frame - previous > _frames(MIN_PAUSE_MS):
            phrases.append((start, previous + 1))
            start = frame
        previous = frame
    phrases.append((start, previous + 1))
    return phrases


def _split_long(phrase: tuple[int, int], rms: np.ndarray, limit: int) -> list[tuple[int, int]]:
    """Cut a phrase longer than *limit* frames at its quietest interior frame.

    Continuous speech with no phrase break still dips between words; cutting
    there is the least bad place to end an excerpt.
    """
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    start, end = phrase
    if end - start <= limit:
        return [phrase]
    margin = max(1, (end - start) // 4)
    cut = start + margin + int(np.argmin(rms[start + margin : end - margin]))
    return _split_long((start, cut), rms, limit) + _split_long((cut, end), rms, limit)


def _window(phrases: list[tuple[int, int]], first: int, last: int, total: int) -> list[tuple[int, int]]:
    """Frame ranges that make up the excerpt of ``phrases[first..last]``.

    Each phrase is kept whole; a pause between two is shortened to
    ``KEEP_PAUSE_MS``, half after one phrase and half before the next; and up
    to ``EDGE_MS`` of the surrounding silence is kept at either end.
    """
    keep, edge = _frames(KEEP_PAUSE_MS), _frames(EDGE_MS)
    floor = phrases[first - 1][1] if first > 0 else 0
    ceiling = phrases[last + 1][0] if last + 1 < len(phrases) else total
    ranges = []
    start = max(floor, phrases[first][0] - edge)
    for index in range(first, last):
        end, following = phrases[index][1], phrases[index + 1][0]
        if following - end > keep:
            ranges.append((start, end + keep // 2))
            start = following - (keep - keep // 2)
    ranges.append((start, min(ceiling, phrases[last][1] + edge)))
    return ranges


def excerpt_ranges(wav: np.ndarray) -> list[tuple[int, int]]:
    """``[start, end)`` sample ranges of the best reference excerpt in *wav*.

    Scores every run of consecutive phrases that fits ``TARGET_SECONDS`` once
    its pauses are shortened, by how much speech it holds, and keeps the best
    (the earliest on a tie). A run that starts and ends at a break is preferred
    while it holds ``BREAK_PREFERENCE`` of the best run's speech. Raises
    :class:`NoUsableExcerpt` when even the best run holds less than
    ``MIN_SPEECH_SECONDS``.
    """
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    mask = silence.speech_mask(audio)
    rms = silence.frame_rms(audio, FRAME, FRAME)
    limit = int(TARGET_SECONDS * SAMPLE_RATE / FRAME)
    phrases = [piece for phrase in _phrases(mask) for piece in _split_long(phrase, rms, limit)]
    gaps = [b[0] - a[1] for a, b in zip(phrases, phrases[1:])]  # gaps[i] follows phrases[i]
    brk = _frames(BREAK_PAUSE_MS)
    opens = [i == 0 or gaps[i - 1] >= brk for i in range(len(phrases))]
    closes = [i == len(phrases) - 1 or gaps[i] >= brk for i in range(len(phrases))]
    voiced = np.concatenate([[0], np.cumsum(mask, dtype=np.int64)])
    enough = MIN_SPEECH_SECONDS * SAMPLE_RATE / FRAME

    def best(at_breaks: bool) -> tuple[int, int, int] | None:
        found: tuple[int, int, int] | None = None  # (speech frames, first, last)
        for first in range(len(phrases)):
            if at_breaks and not opens[first]:
                continue
            for last in range(first, len(phrases)):
                ranges = _window(phrases, first, last, mask.size)
                if sum(end - start for start, end in ranges) > limit:
                    break
                if at_breaks and not closes[last]:
                    continue
                speech = int(voiced[phrases[last][1]] - voiced[phrases[first][0]])
                if found is None or speech > found[0]:
                    found = (speech, first, last)
        return found if found is not None and found[0] >= enough else None

    aligned, anywhere = best(at_breaks=True), best(at_breaks=False)
    chosen = aligned if aligned and anywhere and aligned[0] >= BREAK_PREFERENCE * anywhere[0] else anywhere
    if chosen is None:
        raise NoUsableExcerpt(f"The clip needs at least {MIN_SPEECH_SECONDS:g} seconds of clear speech for advanced cloning.")
    _, first, last = chosen
    return [(start * FRAME, min(end * FRAME, audio.size)) for start, end in _window(phrases, first, last, mask.size)]


def select_excerpt(wav: np.ndarray) -> np.ndarray:
    """The best reference excerpt in *wav*, with its long pauses shortened."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    return np.ascontiguousarray(np.concatenate([audio[start:end] for start, end in excerpt_ranges(audio)]))


def semantic_tokens(signal: np.ndarray) -> list[int]:
    """The excerpt's BiCodec semantic token indices, 50 per second."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.ascontiguousarray(np.asarray(signal, dtype=np.float32).reshape(1, -1))
    session = onnx_runtime.load(catalog.semantic_tokenizer_path())
    raw = session.run(["semantic_tokens"], {"wav": audio})[0]
    tokens = [int(value) for value in np.asarray(raw).reshape(-1)]
    expected = audio.shape[1] // FRAME
    if abs(len(tokens) - expected) > 2:  # a re-export that changed the frame rate
        raise RuntimeError(f"semantic tokenizer returned {len(tokens)} tokens for {expected} frames")
    return validate_reference_tokens(tokens)


def release() -> None:
    """Drop the tokenizer's session; it is large and used once per voice."""
    onnx_runtime.release(catalog.semantic_tokenizer_path())


__all__ = [
    "MIN_SPEECH_SECONDS",
    "NoUsableExcerpt",
    "Reference",
    "SEMANTIC_RATE",
    "TARGET_SECONDS",
    "excerpt_ranges",
    "release",
    "select_excerpt",
    "semantic_tokens",
]
