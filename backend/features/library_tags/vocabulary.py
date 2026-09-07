"""The curated tag vocabulary: normalization, identity, and diffing. Pure."""

from __future__ import annotations

import hashlib
import re

# 64 tags is the ceiling the whole feature is sized around: it is what fits in a
# system prompt without crowding out the instructions, what a chip row can show
# in a 600px modal, and what a JSON-Schema enum stays legible at. It is also the
# reason the library filter had to stop being a 31-bit mask.
MAX_VOCABULARY = 64

# Long enough for "Slow burn romance", short enough that one pathological paste
# cannot dominate the shared prefix every card's call rides on.
MAX_TAG_LENGTH = 40

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_vocabulary(names: list[str]) -> list[str]:
    """The user's raw chip list as the canonical vocabulary.

    Case-insensitive dedupe keeping the first casing: the model is told one
    spelling per concept, and the library filter lowercases anyway, so keeping
    both ``Fantasy`` and ``fantasy`` would spend two of the 64 slots on one tag
    and split its assignments across them.

    ``|`` is stripped rather than escaped because it delimits the filter's
    ``data-tags`` attribute (``|fantasy|romance|``); a tag containing one would
    silently match as two.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        if not isinstance(raw, str):
            continue
        name = _WHITESPACE_RE.sub(" ", raw.replace("|", "")).strip()[:MAX_TAG_LENGTH].strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
        if len(out) >= MAX_VOCABULARY:
            break
    return out


def vocabulary_hash(names: list[str]) -> str:
    """Identity of a vocabulary, for the staleness rule.

    Case-*sensitive* on purpose: the canonical casing is what gets stored on
    every card and shown in the chip row, so editing ``NSFW`` to ``Nsfw`` is a
    real change even though it adds no concept. Order is included for the same
    reason it is stored — but a reorder is handled by the caller's diff (no tag
    added ⇒ bump the hash without re-running), so paying a hash change for it
    costs nothing.
    """
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def diff_vocabulary(old: list[str], new: list[str]) -> tuple[set[str], set[str]]:
    """``(added, removed)`` between two normalized vocabularies.

    Compared case-insensitively so a pure re-casing counts as neither: it
    changes the hash (above), which is enough to restamp the rows, but it cannot
    make a card eligible for a tag it was not already offered.
    """
    old_map = {name.lower(): name for name in old}
    new_map = {name.lower(): name for name in new}
    added = {new_map[k] for k in new_map.keys() - old_map.keys()}
    removed = {old_map[k] for k in old_map.keys() - new_map.keys()}
    return added, removed
