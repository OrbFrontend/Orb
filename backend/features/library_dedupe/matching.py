"""Pure duplicate matching: blocking, pair scoring, reasons, and grouping.

Deliberately free of I/O and of Pillow.  Everything here is a function of the
signals ``fingerprint.py`` produced, so the whole tier/threshold argument is
unit-testable without a database, an image decoder, or a card on disk.

The five thresholds are named module constants rather than literals buried in
the predicates: they are the part of this feature that gets tuned against a real
library, and tuning must be a one-line change rather than a hunt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# ── Thresholds ───────────────────────────────────────────────────────────────

# Token-shingle Jaccard at which two bodies are "the same text, lightly edited".
STRONG_JACCARD = 0.9

# ...and at which they are worth a look but not worth asserting.
POSSIBLE_JACCARD = 0.6

# A shared name *and* creator is corroboration, not proof, so it needs less text
# agreement than a bare Jaccard match — but still some.
NAME_CREATOR_JACCARD = 0.4

# Hamming distance between two 64-bit dHashes that still counts as "same art".
# Measured on deliberately high-frequency synthetic card art (512x768): a JPEG
# q82 re-encode costs 1, WebP q80 costs 2, a 3x downscale costs 1, and a plain
# 2x downscale — exactly what a different download site does — already costs 5.
# So the threshold is 8, not the textbook 4; real card art is smoother and
# should score lower, which leaves headroom without being loose.
AVATAR_MAX_DISTANCE = 8

# Bottom-k shingle sketch size. Two cards sharing any of their 64 smallest
# shingle hashes become candidates, which is the only route that catches a
# near-copy that was also renamed *and* re-avatared.
SKETCH_SIZE = 64

# Above this many members a block stops emitting every pair and emits a chain
# instead. A block is a set of cards agreeing exactly on one value, so the
# predicate that formed it is transitive across the whole block: chaining still
# unions the group and still reports the shared value, at O(n) instead of
# O(n^2). Without it one popular copy-pasted system prompt across 500 cards is
# 124k pairs to score.
MAX_BLOCK = 32

# Fields that feed the signature. Organizational fields (tags, public profiles,
# world_id, creator_notes, timestamps, auto-tag stamps) are excluded on purpose:
# they must never block a match, though they still appear in the comparison.
IDENTITY_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
    "alternate_greetings",
)

STRONG = "strong"
POSSIBLE = "possible"


@dataclass(frozen=True)
class CardSignals:
    """Everything the matcher knows about one card.

    ``field_hashes`` holds only the fields whose normalized value is non-empty.
    That is load-bearing: most cards ship a blank ``system_prompt``, ``scenario``
    and ``mes_example``, so indexing the hash of "" would drop the whole library
    into one block and generate a flood of "same system prompt" reasons for cards
    that merely each left it empty.
    """

    card_id: str
    name: str = ""  # normalized
    creator: str = ""  # normalized
    body_hash: str = ""  # "" when every identity field is empty
    field_hashes: Mapping[str, str] = field(default_factory=dict)
    shingles: frozenset[int] = frozenset()
    sketch: tuple[int, ...] = ()
    avatar_dhash: str = ""  # "" = no avatar, or one that would not decode


@dataclass(frozen=True)
class Pair:
    """One scored pair of cards, ordered canonically (``a`` < ``b``)."""

    a: str
    b: str
    tier: str  # STRONG, POSSIBLE, or "" for no match
    jaccard: float
    avatar_distance: int | None  # None when either side has no usable avatar
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "tier": self.tier,
            "jaccard": round(self.jaccard, 4),
            "avatar_distance": self.avatar_distance,
            "reasons": list(self.reasons),
        }


# ── Blocking ─────────────────────────────────────────────────────────────────


def build_blocks(signals: Sequence[CardSignals]) -> dict[tuple[str, str], list[str]]:
    """Inverted indexes over every signal worth blocking on.

    2000 cards is 2M pairs and pure-Python Jaccard over all of them is minutes,
    so only cards that land in a shared bucket are ever scored. Empty values are
    skipped everywhere — a blank field is not evidence of anything, and indexing
    it is both a quadratic blow-up and a false-positive factory.
    """
    blocks: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in signals:
        if s.body_hash:
            blocks[("body", s.body_hash)].append(s.card_id)
        for name, digest in s.field_hashes.items():
            if digest:
                blocks[(f"field:{name}", digest)].append(s.card_id)
        if s.name:
            blocks[("name", s.name)].append(s.card_id)
            if s.creator:
                blocks[("creator+name", f"{s.creator}\x00{s.name}")].append(s.card_id)
        # Pigeonhole over 8 bands of 8 bits: two hashes within Hamming 7 must
        # agree on at least one band, and at 8 the recall loss is negligible.
        # A card with no usable avatar stores "" and is excluded here and from
        # the "same avatar" predicate — otherwise every avatarless card reads as
        # sharing an avatar with every other, which is the single largest
        # false-positive source this feature has available to it.
        if s.avatar_dhash:
            for i in range(8):
                blocks[(f"avatar{i}", s.avatar_dhash[i * 2 : i * 2 + 2])].append(s.card_id)
        for h in s.sketch:
            blocks[("shingle", format(h, "016x"))].append(s.card_id)
    return dict(blocks)


def candidate_pairs(signals: Sequence[CardSignals]) -> set[tuple[str, str]]:
    """Canonically ordered pairs that share at least one block."""
    pairs: set[tuple[str, str]] = set()
    for members in build_blocks(signals).values():
        if len(members) < 2:
            continue
        members = sorted(set(members))
        if len(members) <= MAX_BLOCK:
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    pairs.add((a, b))
        else:
            for a, b in zip(members, members[1:], strict=False):
                pairs.add((a, b))
    return pairs


# ── Scoring ──────────────────────────────────────────────────────────────────


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    """Exact Jaccard over two shingle sets; 0.0 when either is empty."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    return intersection / (len(a) + len(b) - intersection)


def hamming(a: str, b: str) -> int | None:
    """Bit distance between two hex dHashes, or None if either is missing."""
    if not a or not b:
        return None
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _same_field(a: CardSignals, b: CardSignals, name: str) -> bool:
    """Whether both cards carry the same *non-empty* value for *name*."""
    left = a.field_hashes.get(name, "")
    return bool(left) and left == b.field_hashes.get(name, "")


def _same_name(a: CardSignals, b: CardSignals) -> bool:
    return bool(a.name) and a.name == b.name


def _same_creator(a: CardSignals, b: CardSignals) -> bool:
    return bool(a.creator) and a.creator == b.creator


def reasons_for(a: CardSignals, b: CardSignals, *, overlap: float, distance: int | None) -> tuple[str, ...]:
    """Plain-language reasons, most specific first.

    Generated from which predicates fired rather than from the tier, so a reader
    is told what the scan actually saw. Only ever called for a pair that already
    matched on something, so "Same name" can appear here as corroboration
    without a bare name ever producing a match on its own.
    """
    same_avatar = distance is not None and distance <= AVATAR_MAX_DISTANCE
    reasons: list[str] = []

    if a.body_hash and a.body_hash == b.body_hash:
        reasons.append("Identical content")
    elif _same_field(a, b, "description") and _same_field(a, b, "first_mes"):
        reasons.append("Same description and greeting")
    else:
        if _same_field(a, b, "description"):
            reasons.append("Same description")
        elif _same_field(a, b, "first_mes"):
            reasons.append("Same opening greeting")
        if overlap >= POSSIBLE_JACCARD:
            reasons.append(f"Nearly identical text ({round(overlap * 100)}% overlap)")

    if same_avatar and overlap < POSSIBLE_JACCARD:
        reasons.append("Same avatar, different text")
    elif same_avatar:
        reasons.append("Same avatar")
    elif distance is not None and overlap >= STRONG_JACCARD:
        reasons.append("Same character with a different avatar")

    if _same_name(a, b) and _same_creator(a, b):
        reasons.append("Same creator and name")
    elif _same_name(a, b):
        reasons.append("Same name")

    return tuple(reasons)


def score_pair(a: CardSignals, b: CardSignals) -> Pair:
    """Tier one pair. ``tier == ""`` means the pair is not a duplicate.

    A matching name alone never produces a match, per the brief: every strong
    and possible rule below needs either content agreement or the avatar.
    """
    if b.card_id < a.card_id:
        a, b = b, a
    overlap = jaccard(a.shingles, b.shingles)
    distance = hamming(a.avatar_dhash, b.avatar_dhash)
    same_avatar = distance is not None and distance <= AVATAR_MAX_DISTANCE

    if a.body_hash and a.body_hash == b.body_hash:
        tier = STRONG
    elif _same_field(a, b, "description") and _same_field(a, b, "first_mes"):
        tier = STRONG
    elif overlap >= STRONG_JACCARD and (same_avatar or _same_name(a, b)):
        tier = STRONG
    elif overlap >= POSSIBLE_JACCARD:
        tier = POSSIBLE
    elif same_avatar:
        tier = POSSIBLE
    elif _same_name(a, b) and _same_creator(a, b) and overlap >= NAME_CREATOR_JACCARD:
        tier = POSSIBLE
    else:
        tier = ""

    reasons = reasons_for(a, b, overlap=overlap, distance=distance) if tier else ()
    return Pair(a.card_id, b.card_id, tier, overlap, distance, reasons)


# ── Grouping ─────────────────────────────────────────────────────────────────


def group_strong_edges(pairs: Iterable[Pair]) -> list[list[str]]:
    """Union-find over strong edges only, newest-independent and stable.

    Possible edges are deliberately *not* unioned. A weak edge chained through a
    third card turns two unrelated near-misses into one blob nobody can review,
    so they stay standalone pairs.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pair in pairs:
        if pair.tier != STRONG:
            continue
        ra, rb = find(pair.a), find(pair.b)
        if ra != rb:
            # Point the lexicographically smaller root at the larger one, so the
            # forest a given edge set produces does not depend on scan order.
            parent[min(ra, rb)] = max(ra, rb)

    groups: dict[str, list[str]] = defaultdict(list)
    for member in parent:
        groups[find(member)].append(member)
    return [sorted(members) for members in sorted(groups.values(), key=lambda m: min(m))]


def find_duplicates(
    signals: Sequence[CardSignals],
    *,
    dismissed: Mapping[tuple[str, str], tuple[str, str]] | None = None,
) -> dict:
    """Score every candidate pair and split it into strong groups and possible pairs.

    *dismissed* maps a canonical pair to the two ``body_hash`` values recorded
    when it was dismissed. A dismissal lapses the moment either card's current
    body hash differs — that is "revisit only after meaningful changes", and
    because ``body_hash`` excludes tags and public profiles, re-tagging the
    library cannot resurrect a dismissed pair.
    """
    by_id = {s.card_id: s for s in signals}
    dismissed = dismissed or {}
    candidates = candidate_pairs(signals)

    scored: list[Pair] = []
    lapsed = 0
    still_dismissed = 0
    for a_id, b_id in sorted(candidates):
        pair = score_pair(by_id[a_id], by_id[b_id])
        if not pair.tier:
            continue
        stamp = dismissed.get((a_id, b_id))
        if stamp is not None:
            if stamp == (by_id[a_id].body_hash, by_id[b_id].body_hash):
                still_dismissed += 1
                continue
            lapsed += 1
        scored.append(pair)

    strong = [p for p in scored if p.tier == STRONG]
    groups = group_strong_edges(strong)
    edges_by_card: dict[str, list[Pair]] = defaultdict(list)
    for pair in strong:
        edges_by_card[pair.a].append(pair)

    grouped = []
    for members in groups:
        member_set = set(members)
        grouped.append(
            {
                "cards": members,
                "pairs": [p.as_dict() for m in members for p in edges_by_card[m] if p.b in member_set],
            }
        )

    return {
        "groups": grouped,
        "pairs": [p.as_dict() for p in scored if p.tier == POSSIBLE],
        "stats": {
            "total": len(signals),
            "candidates": len(candidates),
            "strong": len(strong),
            "possible": sum(1 for p in scored if p.tier == POSSIBLE),
            "dismissed": still_dismissed,
            "reopened": lapsed,
        },
    }
