"""Pure helpers for the curated tag vocabulary."""

from __future__ import annotations

import hashlib
import json
import re

MAX_VOCABULARY = 64

MAX_TAG_LENGTH = 40

# Bump when the classifier's prompt, evidence projection, or output contract
# changes in a way that warrants offering existing cards for a fresh pass.
TAGGER_REVISION = 2

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
    """Return the semantic classifier identity of a vocabulary.

    Order and display casing do not change which concepts a card was offered,
    so neither should make a whole library stale.  The explicit tagger revision
    does: it gives prompt/projection upgrades a deliberate invalidation seam.
    """
    payload = {"tagger": TAGGER_REVISION, "tags": sorted(name.lower() for name in names)}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def vocabulary_revision(names: list[str]) -> str:
    """Return the exact order- and casing-sensitive identity used for PUT conflicts."""
    encoded = json.dumps(names, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
