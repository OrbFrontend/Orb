"""Public API for the Character Library's duplicate finder."""

from __future__ import annotations

from .fingerprint import (
    DEDUPE_REVISION,
    MAX_AVATAR_PIXELS,
    SHINGLE_FIELDS,
    SHINGLE_N,
    body_hash,
    dhash_from_image_bytes,
    field_hashes,
    normalize_field,
    shingle_sketch,
    shingles,
    signals_for,
    signals_for_all,
)
from .matching import (
    AVATAR_MAX_DISTANCE,
    IDENTITY_FIELDS,
    MAX_BLOCK,
    NAME_CREATOR_JACCARD,
    POSSIBLE,
    POSSIBLE_JACCARD,
    SKETCH_SIZE,
    STRONG,
    STRONG_JACCARD,
    CardSignals,
    Pair,
    build_blocks,
    candidate_pairs,
    find_duplicates,
    group_strong_edges,
    hamming,
    jaccard,
    reasons_for,
    score_pair,
)
