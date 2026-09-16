"""Shared roleplay conventions and span interpretation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Dialogue(StrEnum):
    QUOTED = "quoted"
    BARE = "bare"
    UNKNOWN = "unknown"


class Narration(StrEnum):
    ASTERISK = "asterisk"
    BARE = "bare"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AxisStyle:
    dialogue: Dialogue
    narration: Narration

    def label(self) -> str:
        return f"dialogue={self.dialogue.value}, narration={self.narration.value}"


def emphasis_inner(raw: str) -> str:
    """Strip surrounding emphasis markers."""
    core = raw.strip()
    if len(core) >= 2 and core[0] in "*_" and core[-1] == core[0]:
        return core[1:-1].strip()
    return core.strip("*_ ").strip()


# A clause can close on stylistic punctuation as well as a full stop: roleplay
# greetings routinely end a line on "~", "♪" or a cut-off dash before an
# action beat. Anything not listed here reads as mid-sentence italics.
_SENTENCE_END = ".!?…~♪♥♡—–-)]"


def is_inline_emphasis(spans: list[tuple[str, int, int]], i: int, para: str) -> bool:
    """Return whether an emphasis span is inline rather than block narration."""
    if i == 0:
        return False
    ptyp, ps, pe = spans[i - 1]
    if ptyp != "NARRATION":
        return False
    left = para[ps:pe].rstrip()
    if not left.strip():
        return False  # whitespace-only gap (e.g. between a quote and the asterisks)
    return left[-1] not in _SENTENCE_END


THOUGHT_ATTRIBUTION = re.compile(
    r"\s*(?:I|[Hh]e|[Ss]he|[Tt]hey|[Ww]e|[Yy]ou|[Tt]he\s+[\w'-]+|[A-Z][\w'-]*)\s+"
    r"(?:thinks?|thought|(?:tells?|told|says?|said|reminds?|reminded|asks?|asked)\s+"
    r"(?:myself|himself|herself|themselves|ourselves|yourself))"
    r"(?=\s*[,.;!?]|\s+(?:with|as|while|\w+ly)\b|\s*$)"
)


def span_role(spans: list[tuple[str, int, int]], i: int, src_dialogue: Dialogue, para: str) -> str:
    """Map a span to its semantic role under the source convention."""
    typ = spans[i][0]
    if typ == "SPEECH":
        return "DIALOGUE"
    if typ == "EMPHASIS":
        return "EMPHASIS_INLINE" if is_inline_emphasis(spans, i, para) else "NARRATION"
    if src_dialogue == Dialogue.BARE:
        return "DIALOGUE"  # asterisk convention: bare runs are spoken lines
    return "NARRATION"


def split_ws(raw: str) -> tuple[str, str, str]:
    """(leading_ws, core, trailing_ws) so a transform touches only the core."""
    lead = raw[: len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()) :]
    return lead, raw.strip(), trail


def strip_quotes(raw: str) -> str:
    lead, core, trail = split_ws(raw)
    if len(core) >= 2:
        core = core[1:-1].strip()
    return f"{lead}{core}{trail}"
