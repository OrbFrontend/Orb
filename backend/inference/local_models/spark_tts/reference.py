"""Choose and encode the reference excerpt for advanced cloning."""

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

#: Maximum excerpt length after pause shortening.
TARGET_SECONDS = 10.0
#: Minimum speech needed for a useful reference.
MIN_SPEECH_SECONDS = 2.0
#: Minimum gap between phrases.
MIN_PAUSE_MS = 200
#: Gap marking a sentence or clause break.
BREAK_PAUSE_MS = 400
#: Minimum speech share required to prefer break-aligned excerpts.
BREAK_PREFERENCE = 0.5
#: Maximum pause kept inside the excerpt.
KEEP_PAUSE_MS = 200
#: Context kept around the excerpt.
EDGE_MS = 100


class NoUsableExcerpt(ValueError):
    """The clip has no usable speech excerpt."""


@dataclass(frozen=True)
class Reference:
    """Reference speech and its transcript."""

    text: str
    tokens: Sequence[int]


def _frames(ms: float) -> int:
    return max(1, round(ms * SAMPLE_RATE / 1000 / FRAME))


def _phrases(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return speech runs, splitting on phrase-length pauses."""
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
    """Split long speech runs at their quietest interior frames."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    start, end = phrase
    if end - start <= limit:
        return [phrase]
    margin = max(1, (end - start) // 4)
    cut = start + margin + int(np.argmin(rms[start + margin : end - margin]))
    return _split_long((start, cut), rms, limit) + _split_long((cut, end), rms, limit)


def _window(phrases: list[tuple[int, int]], first: int, last: int, total: int) -> list[tuple[int, int]]:
    """Return sample-frame ranges for a phrase window with shortened pauses."""
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
    """Return the best ``[start, end)`` sample ranges for *wav*."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    mask = silence.speech_mask(audio)
    rms = silence.frame_rms(audio, FRAME, FRAME)
    limit = int(TARGET_SECONDS * SAMPLE_RATE / FRAME)
    phrases = [piece for phrase in _phrases(mask) for piece in _split_long(phrase, rms, limit)]
    gaps = [b[0] - a[1] for a, b in zip(phrases, phrases[1:])]
    brk = _frames(BREAK_PAUSE_MS)
    opens = [i == 0 or gaps[i - 1] >= brk for i in range(len(phrases))]
    closes = [i == len(phrases) - 1 or gaps[i] >= brk for i in range(len(phrases))]
    voiced = np.concatenate([[0], np.cumsum(mask, dtype=np.int64)])
    enough = MIN_SPEECH_SECONDS * SAMPLE_RATE / FRAME

    def best(at_breaks: bool) -> tuple[int, int, int] | None:
        found: tuple[int, int, int] | None = None
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
    """Return the best reference excerpt with long pauses shortened."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    return np.ascontiguousarray(np.concatenate([audio[start:end] for start, end in excerpt_ranges(audio)]))


def semantic_tokens(signal: np.ndarray) -> list[int]:
    """Encode an excerpt as BiCodec semantic tokens."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.ascontiguousarray(np.asarray(signal, dtype=np.float32).reshape(1, -1))
    session = onnx_runtime.load(catalog.semantic_tokenizer_path())
    raw = session.run(["semantic_tokens"], {"wav": audio})[0]
    tokens = [int(value) for value in np.asarray(raw).reshape(-1)]
    expected = audio.shape[1] // FRAME
    if abs(len(tokens) - expected) > 2:
        raise RuntimeError(f"semantic tokenizer returned {len(tokens)} tokens for {expected} frames")
    return validate_reference_tokens(tokens)


def release() -> None:
    """Release the tokenizer session."""
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
