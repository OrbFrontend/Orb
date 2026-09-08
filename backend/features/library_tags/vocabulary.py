"""Pure helpers for the curated tag vocabulary."""

from __future__ import annotations

import hashlib
import re

MAX_VOCABULARY = 64

MAX_TAG_LENGTH = 40

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_vocabulary(names: list[str]) -> list[str]:
    """Normalize, deduplicate, and cap user-supplied tag names."""
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
    """Return the case- and order-sensitive identity of a vocabulary."""
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def diff_vocabulary(old: list[str], new: list[str]) -> tuple[set[str], set[str]]:
    """Return case-insensitive ``(added, removed)`` vocabulary names."""
    old_map = {name.lower(): name for name in old}
    new_map = {name.lower(): name for name in new}
    added = {new_map[k] for k in new_map.keys() - old_map.keys()}
    removed = {old_map[k] for k in old_map.keys() - new_map.keys()}
    return added, removed
