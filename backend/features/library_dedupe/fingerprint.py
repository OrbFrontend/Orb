"""Content and avatar signals for one character card.

Split from ``matching.py`` the way ``library_tags`` splits ``vocabulary`` from
``tagger``: everything with an outside dependency (Pillow) lives here, and the
matcher stays importable and testable without it.

Text signals are recomputed on every scan and nothing about them is cached.
That is a measurement, not an omission: at 2000 cards of ~400 words, tokenizing,
shingling and sketching the whole library costs 0.26s, while decoding the
avatars costs ~18s (8.8 ms per 1024x1536 PNG). Only the avatar hash is worth a
column, and leaving the text side uncached means threshold tuning needs no cache
rebuild at all.
"""

from __future__ import annotations

import hashlib
import io
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from PIL import Image, UnidentifiedImageError

from ...analysis.text.lexical import ngrams, tokenize
from .matching import IDENTITY_FIELDS, SKETCH_SIZE, CardSignals

# Bump when normalization, the shingle width, or the dHash construction changes
# in a way that makes a stored ``avatar_dhash`` mean something different. It
# salts the stamp, which is the invalidation seam — exactly as TAGGER_REVISION
# salts ``vocabulary_hash()``.
DEDUPE_REVISION = 1

# Token 5-grams: long enough that unrelated prose shares none (a 2000-card
# benchmark over random text produced zero candidate pairs), short enough to
# survive the sentence-level edits a "minor edit" duplicate actually carries.
SHINGLE_N = 5

# Which fields the shingle sketch reads. The narrative body only: a shared
# system prompt or example-message block is a field-hash signal, not evidence
# that two cards are the same character.
SHINGLE_FIELDS = ("description", "personality", "scenario", "first_mes")

# Decompression-bomb guard. Deliberately *not* ``Image.MAX_IMAGE_PIXELS``: that
# is PIL global state, so raising or lowering it here would silently change
# behaviour for ``features/cards/parsing.py`` and the image-generation workflow.
# ``Image.open`` is lazy and reads only the header, so the check below happens
# before ``.convert("L")`` decodes a single row.
MAX_AVATAR_PIXELS = 50_000_000

_DHASH_WIDTH = 9
_DHASH_HEIGHT = 8


def normalize_field(value: Any) -> str:
    """NFC, casefold, collapse whitespace runs to one space, trim.

    One normalizer for every text signal, so "Same description" means the same
    thing to the hash, the block and the reason.
    """
    if isinstance(value, (list, tuple)):
        # ``alternate_greetings``: order is presentation, not identity, and the
        # same greeting listed twice is not a different card.
        parts = sorted({normalize_field(item) for item in value} - {""})
        return "\n".join(parts)
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def field_hashes(card: Mapping[str, Any]) -> dict[str, str]:
    """Per-field digests for the identity fields that are actually filled in.

    Empty fields are omitted rather than hashed. A blank ``scenario`` shared by
    half the library is not a reason, and indexing it would make it one.
    """
    out: dict[str, str] = {}
    for name in IDENTITY_FIELDS:
        normalized = normalize_field(card.get(name))
        if normalized:
            out[name] = _sha(normalized)
    return out


def body_hash(card: Mapping[str, Any]) -> str:
    """One digest over every identity field, or "" when the card has no content.

    Field names are folded into the payload so moving text between two fields
    cannot collide with leaving it where it was. This value is also what a
    dismissal is stamped with, which is why it excludes tags and public
    profiles: re-tagging the library must not resurrect dismissed pairs.
    """
    parts = [f"{name}\x00{normalize_field(card.get(name))}" for name in IDENTITY_FIELDS]
    if not any(part.split("\x00", 1)[1] for part in parts):
        return ""
    return _sha("\x1f".join(parts))


def shingles(card: Mapping[str, Any]) -> frozenset[int]:
    """Stably hashed token 5-grams over the card's narrative body.

    ``blake2b``, not the built-in ``hash()``: Python randomizes string and tuple
    hashing per process, which would make sketches — and therefore any test that
    asserts on them — differ across restarts.
    """
    text = " ".join(normalize_field(card.get(name)) for name in SHINGLE_FIELDS)
    tokens = tokenize(text)
    return frozenset(
        int.from_bytes(hashlib.blake2b(" ".join(gram).encode("utf-8"), digest_size=8).digest(), "big")
        for gram in ngrams(tokens, SHINGLE_N)
    )


def shingle_sketch(values: frozenset[int], k: int = SKETCH_SIZE) -> tuple[int, ...]:
    """The k smallest shingle hashes — a bottom-k sketch used only for blocking.

    Two cards sharing any sketch member become candidates and are then scored on
    their *full* shingle sets, so the sketch costs recall on nothing it indexes
    and never decides a tier.
    """
    return tuple(sorted(values)[:k])


def dhash_from_image_bytes(data: bytes) -> str:
    """64-bit difference hash of *data* as 16 hex chars, or "" if it will not decode.

    Pixels, not bytes: ``avatar_b64`` holds the entire original card PNG with its
    metadata chunks, so a byte hash of it is ~equivalent to the content-addressed
    card id and cannot see "same art, different wrapper".

    Every decode failure returns "" rather than raising, so one corrupt avatar in
    a 2000-card library never fails the scan — and "" is excluded from avatar
    blocking and from the "same avatar" predicate entirely.
    """
    if not data:
        return ""
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if width < 1 or height < 1 or width * height > MAX_AVATAR_PIXELS:
                return ""
            small = im.convert("L").resize((_DHASH_WIDTH, _DHASH_HEIGHT), Image.Resampling.LANCZOS)
            # "L" mode raw bytes are row-major and unpadded, so this is the
            # 9x8 grid flattened — and it is not the deprecated getdata().
            pixels = small.tobytes()
    except (UnidentifiedImageError, OSError, ValueError, MemoryError):
        return ""

    bits = 0
    for row in range(_DHASH_HEIGHT):
        base = row * _DHASH_WIDTH
        for col in range(_DHASH_WIDTH - 1):
            bits = (bits << 1) | int(pixels[base + col] > pixels[base + col + 1])
    return f"{bits:016x}"


def signals_for(card: Mapping[str, Any], avatar_dhash: str = "") -> CardSignals:
    """Assemble one card's matcher input. *avatar_dhash* comes from the cache."""
    values = shingles(card)
    return CardSignals(
        card_id=str(card.get("id") or ""),
        name=normalize_field(card.get("name")),
        creator=normalize_field(card.get("creator")),
        body_hash=body_hash(card),
        field_hashes=field_hashes(card),
        shingles=values,
        sketch=shingle_sketch(values),
        avatar_dhash=avatar_dhash or "",
    )


def signals_for_all(cards: Sequence[Mapping[str, Any]], dhashes: Mapping[str, str]) -> list[CardSignals]:
    """Signals for a whole library, given the cached avatar hashes."""
    return [signals_for(card, dhashes.get(str(card.get("id") or ""), "")) for card in cards]
