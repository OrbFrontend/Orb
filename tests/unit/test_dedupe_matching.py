"""Blocking, tiers, reasons, and grouping for the duplicate finder.

The subject is what the scan is allowed to claim. Each test names one rule from
the brief and the failure it prevents: a name alone is never a duplicate, an
empty field is never evidence, a missing avatar is never a shared avatar, and a
weak edge never chains two unrelated cards into one group.

The bodies below are card-sized on purpose. The Jaccard thresholds are
calibrated against real card prose, and a two-sentence stand-in would make a
one-word edit look like a rewrite.
"""

from __future__ import annotations

from backend.features.library_dedupe import (
    MAX_BLOCK,
    POSSIBLE,
    STRONG,
    CardSignals,
    build_blocks,
    candidate_pairs,
    find_duplicates,
    group_strong_edges,
    hamming,
    jaccard,
    score_pair,
    signals_for,
)

BODY = (
    "She keeps bees at the edge of the wood, in nine white hives she painted herself the summer "
    "the river froze. The villagers come to her for salves and for the other thing, and she gives "
    "them the salves. Her hands are scarred from the smoker and she has never once been stung, "
    "which she says is luck and everyone else says is something worse. She was born in the valley "
    "and has left it exactly twice, both times before the war, and she will tell you about neither "
    "trip. In the winter she reads by one candle to save the tallow, and in the spring she is out "
    "before light, walking the rows and listening for the hum that means a hive has gone queenless. "
    "She keeps a ledger of every swarm she has ever taken, written in a hand nobody else can read. "
    "There is a second ledger she keeps in the floor under the bed, and that one is not about bees."
)

OTHER_BODY = (
    "He unloads salt barges before dawn on the third pier, where the crane has been broken since "
    "the spring and nobody has come to look at it. The work is paid by the hundredweight and he is "
    "faster than anyone on the wharf, which has made him exactly no friends. He sleeps in the loft "
    "above the ropewalk and pays for it in labour, splicing until his hands crack. Twice a month a "
    "letter arrives for him from the interior and he burns it unopened in the brazier by the gate."
)

# One phrase changed — the "user edited their copy" duplicate.
LIGHT_EDIT = BODY.replace("nine white hives", "eleven white hives")

AVATAR = "18aa4618625518e7"
NEAR_AVATAR = "18aa4618625518e6"  # one bit away: the same art, re-encoded
FAR_AVATAR = "ffff0000ffff0000"


def _card(card_id: str, **overrides) -> dict:
    card = {
        "id": card_id,
        "name": "Lira",
        "description": BODY,
        "personality": "Dry, patient, unwilling to explain herself twice.",
        "scenario": "",
        "first_mes": "*She does not look up from the hive.* You are late.",
        "mes_example": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "creator": "",
    }
    card.update(overrides)
    return card


def _signals(card_id: str, *, avatar: str = "", **overrides) -> CardSignals:
    return signals_for(_card(card_id, **overrides), avatar)


# A distinct voice per name, so a helper's own boilerplate can never be the
# thing two "unrelated" cards turn out to share.
_VOICES = {
    "Lira": (
        "Dry, patient, unwilling to explain herself twice.",
        "*She does not look up from the hive.* You are late.",
    ),
    "Toma": (
        "Blunt past the point of rudeness, and generous only with strangers.",
        "*He drops the sack and rolls one shoulder.* Mind the rope, it whips.",
    ),
    "Sef": (
        "Fussy about ink and careless about everything else in the world.",
        "*The pen keeps moving.* Sit down, this line has to close first.",
    ),
    "Wrenna": (
        "Cheerful in the particular way that makes other people nervous.",
        "*Already grinning at nothing.* Oh good. Someone I have not met.",
    ),
}


def _unrelated(card_id: str, name: str, body: str, **overrides) -> CardSignals:
    """A card that shares nothing with the others — not even a helper's phrasing."""
    personality, first_mes = _VOICES[name]
    return _signals(card_id, name=name, description=body, personality=personality, first_mes=first_mes, **overrides)


# ── What is never a duplicate ────────────────────────────────────────────────


def test_a_shared_name_alone_is_not_a_duplicate():
    """The brief's one explicit non-rule.

    Two unrelated characters called Lira are not the same card, and a library
    that flags them teaches the user to ignore the whole tool.
    """
    a = _unrelated("a", "Lira", BODY)
    b = _unrelated("b", "Lira", OTHER_BODY)
    assert score_pair(a, b).tier == ""


def test_a_shared_name_and_creator_still_need_text_agreement():
    # Creator + name is corroboration, not proof: one prolific creator's whole
    # catalogue would otherwise pair with itself on the two cards that happen to
    # share a protagonist name.
    a = _unrelated("a", "Lira", BODY, creator="mothwood")
    b = _unrelated("b", "Lira", OTHER_BODY, creator="mothwood")
    assert score_pair(a, b).tier == ""


def test_two_cards_with_no_avatar_do_not_count_as_sharing_one():
    """The single largest false-positive source available to this feature.

    An avatarless card stores "", and "" must be excluded from avatar blocking
    and from the "same avatar" predicate — otherwise every avatarless card in the
    library reads as sharing an avatar with every other one.
    """
    a = _unrelated("a", "Lira", BODY)
    b = _unrelated("b", "Toma", OTHER_BODY)
    assert a.avatar_dhash == "" and b.avatar_dhash == ""
    assert hamming(a.avatar_dhash, b.avatar_dhash) is None
    assert score_pair(a, b).tier == ""
    assert not any(key[0].startswith("avatar") for key in build_blocks([a, b]))


def test_two_cards_with_an_empty_system_prompt_are_not_matched_on_it():
    """A blank field shared by half the library is not evidence.

    If empty-valued hashes were indexed, every such card would land in one giant
    block — an O(n^2) blow-up inside it and a flood of nonsense reasons.
    """
    a = _unrelated("a", "Lira", BODY)
    b = _unrelated("b", "Toma", OTHER_BODY)
    assert "system_prompt" not in a.field_hashes and "scenario" not in a.field_hashes
    assert not any(key[0] in ("field:system_prompt", "field:scenario") for key in build_blocks([a, b]))
    assert score_pair(a, b).tier == ""


def test_a_blank_creator_never_forms_a_block():
    # The same rule for the block that pairs creator with name: nearly every
    # imported card leaves creator empty.
    assert not any(key[0] == "creator+name" for key in build_blocks([_signals("a"), _signals("b")]))


# ── Strong matches ───────────────────────────────────────────────────────────


def test_identical_content_is_a_strong_match_whatever_the_tags_are():
    # The headline case: the same card imported twice from two sites, differing
    # only in the tags one of them attached.
    pair = score_pair(_signals("a", tags=["Fantasy"]), _signals("b", tags=[]))
    assert pair.tier == STRONG
    assert pair.reasons[0] == "Identical content"


def test_the_same_description_and_greeting_is_strong_even_with_a_new_name():
    # A user renamed their copy. Two whole fields agreeing exactly is not a
    # coincidence, so this does not wait for the Jaccard threshold.
    pair = score_pair(_signals("a"), _signals("b", name="Lira of the Hives", personality="Rewritten from scratch."))
    assert pair.tier == STRONG
    assert "Same description and greeting" in pair.reasons


def test_a_lightly_edited_copy_with_the_same_avatar_is_strong():
    a = _signals("a", avatar=AVATAR)
    b = _signals("b", avatar=NEAR_AVATAR, name="Someone Else", description=LIGHT_EDIT)
    pair = score_pair(a, b)
    assert pair.tier == STRONG
    assert pair.jaccard >= 0.9
    assert "Same avatar" in pair.reasons


def test_high_text_overlap_with_a_different_avatar_says_so():
    # "Same character with a different avatar" is a reason the brief asks for by
    # name: the text agrees, the art does not.
    a = _signals("a", avatar=AVATAR)
    b = _signals("b", avatar=FAR_AVATAR, description=LIGHT_EDIT)
    pair = score_pair(a, b)
    assert pair.tier == STRONG  # the name still matches and the text is 0.9+
    assert "Same character with a different avatar" in pair.reasons


# ── Possible matches ─────────────────────────────────────────────────────────


def test_the_same_avatar_with_different_text_is_only_possible():
    a = _unrelated("a", "Lira", BODY, avatar=AVATAR)
    b = _unrelated("b", "Toma", OTHER_BODY, avatar=NEAR_AVATAR)
    pair = score_pair(a, b)
    assert pair.tier == POSSIBLE
    assert "Same avatar, different text" in pair.reasons


def test_moderate_text_overlap_is_possible_and_reports_the_percentage():
    truncated = ". ".join(BODY.split(". ")[:6]) + ". The tide takes the boats out twice a day and the gulls follow."
    a = _unrelated("a", "Lira", BODY)
    b = _unrelated("b", "Wrenna", truncated)
    pair = score_pair(a, b)
    assert pair.tier == POSSIBLE
    assert any(reason.startswith("Nearly identical text (") for reason in pair.reasons)


def test_a_renamed_and_re_avatared_copy_stays_possible_rather_than_strong():
    # Nothing corroborates the text: not the name, not the art, not a whole
    # field. High overlap alone is a lead, not a verdict.
    a = _signals("a", avatar=AVATAR)
    b = _signals("b", name="Wrenna", avatar=FAR_AVATAR, description=LIGHT_EDIT)
    pair = score_pair(a, b)
    assert pair.jaccard >= 0.9  # the text alone would clear the strong bar
    assert pair.tier == POSSIBLE


# ── Blocking ─────────────────────────────────────────────────────────────────


def test_a_renamed_near_copy_is_still_a_candidate_via_the_shingle_sketch():
    """The only route that catches a copy that was renamed *and* re-avatared.

    No field hash matches, no name matches, and the avatars are far apart — the
    bottom-k sketch is the sole reason these two are ever scored at all.
    """
    a = _signals("a", avatar=AVATAR)
    b = _unrelated("b", "Wrenna", LIGHT_EDIT, avatar=FAR_AVATAR)

    assert not set(a.field_hashes.values()) & set(b.field_hashes.values())
    assert ("a", "b") in candidate_pairs([a, b])
    assert any(key[0] == "shingle" and len(members) == 2 for key, members in build_blocks([a, b]).items())
    assert score_pair(a, b).tier == POSSIBLE


def test_unrelated_cards_produce_no_candidate_pairs_at_all():
    # The blocking has to be quiet as well as cheap: over 2000 cards of unrelated
    # text it produced zero pairs, which is what makes recomputing every text
    # signal on each scan affordable.
    cards = [
        _unrelated("a", "Lira", BODY),
        _unrelated("b", "Toma", OTHER_BODY),
        _unrelated("c", "Sef", "A cartographer who has never once left the city whose every street she has mapped."),
    ]
    assert candidate_pairs(cards) == set()


def test_an_oversized_block_degrades_to_a_chain_instead_of_every_pair():
    """A block is a set of cards agreeing exactly, so the predicate is transitive.

    Chaining still unions the whole group and still reports the shared value, at
    O(n) instead of O(n^2) — without it one copy-pasted system prompt across the
    library is a quadratic scoring bill.
    """
    members = [_signals(f"card-{i:03d}") for i in range(MAX_BLOCK + 5)]
    by_id = {m.card_id: m for m in members}
    pairs = candidate_pairs(members)
    assert len(pairs) == len(members) - 1
    # ...and the chain is still enough to collapse them into one strong group.
    scored = [score_pair(by_id[a], by_id[b]) for a, b in pairs]
    assert group_strong_edges(scored) == [sorted(by_id)]


# ── Grouping ─────────────────────────────────────────────────────────────────


def test_possible_edges_do_not_chain_unrelated_cards_into_one_group():
    """Union-find runs over strong edges only.

    A weak edge chained through a third card turns two unrelated near-misses
    into one blob nobody can review, which is worse than reporting nothing.
    """
    a = _unrelated("a", "Lira", BODY, avatar=AVATAR)
    b = _unrelated("b", "Toma", OTHER_BODY, avatar=NEAR_AVATAR)
    c = _unrelated("c", "Sef", "A cartographer who has never left the city whose streets she maps.", avatar="18aa4618625518e5")

    result = find_duplicates([a, b, c])
    assert result["groups"] == []
    assert len(result["pairs"]) == 3
    assert {p["tier"] for p in result["pairs"]} == {POSSIBLE}


def test_strong_edges_do_collapse_a_trio_into_one_group():
    result = find_duplicates([_signals("a"), _signals("b"), _signals("c")])
    assert [g["cards"] for g in result["groups"]] == [["a", "b", "c"]]
    assert result["stats"]["strong"] == 3 and result["stats"]["possible"] == 0
    # Every in-group edge is reported, so the panel can name a reason per pair.
    assert {(p["a"], p["b"]) for p in result["groups"][0]["pairs"]} == {("a", "b"), ("a", "c"), ("b", "c")}


def test_jaccard_of_an_empty_shingle_set_is_zero_rather_than_undefined():
    # A one-line card produces no 5-grams. It must score 0.0, not divide by zero
    # and not read as a perfect match against another empty one.
    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({1, 2}), frozenset()) == 0.0


# ── Dismissals ───────────────────────────────────────────────────────────────


def test_a_dismissed_pair_is_withheld_until_a_card_actually_changes():
    a, b = _signals("a"), _signals("b")
    dismissed = {("a", "b"): (a.body_hash, b.body_hash)}

    held = find_duplicates([a, b], dismissed=dismissed)
    assert held["groups"] == [] and held["stats"]["dismissed"] == 1

    # "Revisit only after meaningful changes": the stamp is the body hash, so an
    # edit lapses the dismissal and a re-tag cannot.
    back = find_duplicates([a, _signals("b", description=LIGHT_EDIT)], dismissed=dismissed)
    assert [g["cards"] for g in back["groups"]] == [["a", "b"]]
    assert back["stats"]["reopened"] == 1
