"""Turn Whisper token ids back into text."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping


def _byte_decoder() -> dict[str, int]:
    """Invert GPT-2's byte-to-printable-character table."""
    printable = [*range(ord("!"), ord("~") + 1), *range(ord("¡"), ord("¬") + 1), *range(ord("®"), ord("ÿ") + 1)]
    chars = list(printable)
    shifted = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            chars.append(256 + shifted)
            shifted += 1
    return {chr(char): byte for byte, char in zip(printable, chars, strict=True)}


_BYTES = _byte_decoder()


class Vocabulary:
    """The text tokens from a Whisper ``vocab.json``."""

    def __init__(self, token_ids: Mapping[str, int]) -> None:
        self._text = {int(index): token for token, index in token_ids.items()}

    @classmethod
    def load(cls, path: str) -> Vocabulary:
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self._text)

    def decode(self, ids: Iterable[int], *, skip: Iterable[int] = ()) -> str:
        """Join the tokens for *ids*, leaving out *skip* and unknown ids."""
        dropped = set(skip)
        spelled = "".join(self._text.get(i, "") for i in ids if i not in dropped)
        raw = bytes(_BYTES[char] for char in spelled if char in _BYTES)
        return raw.decode("utf-8", errors="replace")


__all__ = ["Vocabulary"]
