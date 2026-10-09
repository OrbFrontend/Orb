import pytest

from backend.analysis.text.markup import classify_axes
from backend.analysis.text.roleplay import AxisStyle, Dialogue, Narration
from backend.workflows.format_consistency.normalization import (
    baseline_axes,
    normalize_format,
    normalize_to_baseline,
    skip_reasons,
    stable_label,
    vote_axes,
)

QUOTES_ONLY = 'She smiles and steps back. "I won\'t go," she says, turning to the window.'
ASTERISKS_ONLY = "*She smiles and steps back, turning to the window.* I won't go."
FULL_MARKUP = '*He leans on the doorframe, arms crossed.* "You came back," *he murmurs.*'
QUOTES_BASELINE = ['She smiles. "Hello there," she says warmly.', 'He nods. "Welcome back," he replies.']
GREETING = ['She smiles. "Hello there."']
FULL_MARKUP_BASELINE = [
    '*He leaned against the bookshelf, watching her.* "You came back," *he murmured.*',
    '*She set down the book, fingers trembling.* "I told you I would," *she said softly.*',
    '*He crossed the room in three steps.* "Then prove it," *he whispered.*',
]
BARE_NARRATION_BASELINE = [
    'She set the book down. "I told you I would," she said softly.',
    'He crossed the room in three steps. "Then prove it," he whispered.',
    'She looked away. "It doesnae matter," she muttered.',
]
# A bare-narration chat that sets its thoughts in asterisks.
THOUGHT_IN_ASTERISKS_BASELINE = [
    'She set the book down. *He never listens.* "I told you I would," she said softly.',
    *BARE_NARRATION_BASELINE[1:],
]
HALF_ASTERISKED_DRAFT = (
    "Amaryllis blinked, caught off guard by how serious the question was.\n\n"
    "Her fingers tightened around the spine of the book.\n\n*She let out a short, breathy laugh, the first in weeks.*\n\n"
    '"Battle scenes?" *she asked, her accent lilting.* "Aye, there\'s a few."\n\n'
    '*She cleared her throat and looked away.* "There\'s a siege in the third act."'
)
THOUGHT_IN_QUOTED_PROSE = 'She crossed the room slowly. *He still loves me. He has to.* "Good night," she said.'
FIRST_PERSON_BEAT = (
    "*I smile kindly at you.* Hello, Kit. Thank you for coming to our club. "
    "As president of the Literature Club, it's my duty to make the club fun and "
    "exciting for everyone! Tell me, what brings you here today?"
)
HEIDI_GREETING = (
    "Hello, Kit. Thank you for coming to our club. As president of the Literature "
    "Club, it's my duty to make the club fun and exciting for everyone! "
    "*Heidi smiles kindly at you.* Tell me, what brings you here today?"
)


def _assert_unchanged(draft: str, base: list[str], *, enabled: bool = True):
    new, report = normalize_to_baseline(draft, base, enabled=enabled)
    assert not report.changed
    assert new == draft
    return report


def _converted(draft: str, base: list[str], **kwargs) -> str:
    new, report = normalize_to_baseline(draft, base, enabled=True, **kwargs)
    assert report.changed
    return new


CONVERSIONS = {
    # The reported inversion, both ways.
    "asterisk_draft_to_quotes_baseline": (
        "*She steps closer, watching him carefully.* Are you sure about this?",
        ['She smiles. "Hello there," she says warmly.'],
        'She steps closer, watching him carefully. "Are you sure about this?"',
    ),
    "quotes_draft_to_asterisk_baseline": (
        'She steps closer, watching him. "Are you sure about this?"',
        ["*She smiles, stepping back toward the window.* Hello there."],
        "*She steps closer, watching him.* Are you sure about this?",
    ),
    # Only the drifted axis is touched.
    "only_narration_axis_changes_quotes_preserved": (
        'He leans against the wall. "You came back."',
        ['*He leans against the doorframe, arms crossed.* "You came back," *he murmurs.*'],
        '*He leans against the wall.* "You came back."',
    ),
    # Protected asterisk runs (markdown bold-italic) survive while the prose normalizes.
    "bold_italic_run_preserved": (
        '*He leans in close.* "You came back." ***He could not believe it.***',
        QUOTES_BASELINE,
        'He leans in close. "You came back." ***He could not believe it.***',
    ),
    # Fenced code is literal content, never reformatted.
    "code_block_passes_through_verbatim": (
        '*He leans in.* "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```',
        QUOTES_BASELINE,
        'He leans in. "You came back."\n\n```python\nx = a ***b*** c  # not RP markup\n```',
    ),
    # *emphasis* inside dialogue survives the narration strip.
    "emphasis_in_dialogue_survives": (
        '*He leans in.* "Do you think I am *stupid*?"',
        GREETING,
        'He leans in. "Do you think I am *stupid*?"',
    ),
    "inline_emphasis_inside_narration_not_fragmented": (
        '"Where is he?" She was *really* nervous about the whole thing.',
        ['*He waited by the window, tense.* "Where were you?"'],
        '"Where is he?" *She was really nervous about the whole thing.*',
    ),
    "asterisk_narration_without_quotes_reads_as_bare_dialogue": (
        "*She steps closer.* Are you sure? *She hesitates.* Really sure?",
        GREETING,
        'She steps closer. "Are you sure?" She hesitates. "Really sure?"',
    ),
    "wrapping_bare_narration_does_not_swallow_a_marked_span": (
        'She walked. *He still loves me.* She stopped. "Hello," she said.',
        FULL_MARKUP_BASELINE,
        '*She walked.* *He still loves me.* *She stopped.* "Hello," *she said.*',
    ),
    # Only the paragraph with an unpaired quote is skipped.
    "only_the_paragraph_with_the_unpaired_quote_is_skipped": (
        'She pours the tea. "Drink it while it\'s hot."\n\n"Hello, she said. "Bye," he replied.',
        FULL_MARKUP_BASELINE,
        '*She pours the tea.* "Drink it while it\'s hot."\n\n"Hello, she said. "Bye," he replied.',
    ),
    "measurement_marks_and_apostrophes_do_not_block_a_rewrite": (
        'He is 6\'2" and the dogs’ keeper. "Sit down," he says.',
        FULL_MARKUP_BASELINE,
        '*He is 6\'2" and the dogs’ keeper.* "Sit down," *he says.*',
    ),
    "an_ornamental_quote_is_speech_to_the_rewriter_too": (
        "She leans closer. ❝You came back,❞ she murmurs.",
        FULL_MARKUP_BASELINE,
        "*She leans closer.* ❝You came back,❞ *she murmurs.*",
    ),
}


@pytest.mark.parametrize("draft,base,expected", CONVERSIONS.values(), ids=CONVERSIONS.keys())
def test_a_drifting_draft_converts_to_the_baseline(draft, base, expected):
    assert _converted(draft, base) == expected


CONTAINS = {
    "contractions_survive": (
        "She can't believe it. \"I won't leave,\" she insists.",
        ["*She waves.* Hi there."],
        ["can't", "won't"],
        [],
    ),
    "emphasis_survives_dialogue_flattening": (
        'He frowns. "Do you think I am *stupid*?"',
        ["*She smiles, stepping back.* Hello there.", "*He follows her in.* Good to see you."],
        ["*stupid*"],
        [],
    ),
    "stray_bare_dialogue_tag_is_wrapped_when_dominant_style_matches": (
        '*She pulled the curtain aside and glanced out at the street.*\n\n"It looks like it might rain," she replied.\n\n'
        "*She let the fabric fall back and turned to the window latch.*",
        FULL_MARKUP_BASELINE,
        [
            '"It looks like it might rain," *she replied.*',
            "*She pulled the curtain aside and glanced out at the street.*",
            "*She let the fabric fall back and turned to the window latch.*",
        ],
        [],
    ),
    "quoteless_full_markup_draft_wraps_bare_paragraph_in_asterisks": (
        "*Jane traced a finger along the spines of the books on the shelf.*\n\n"
        "The librarian tilted her head, her blue eyes scanning the titles.\n\n"
        "*She slid one volume free and weighed it in her hands.*",
        FULL_MARKUP_BASELINE,
        ["*The librarian tilted her head, her blue eyes scanning the titles.*"],
        ['"'],
    ),
    "half_asterisked_draft_is_completed_against_an_asterisk_baseline": (
        HALF_ASTERISKED_DRAFT,
        FULL_MARKUP_BASELINE,
        [
            "*Amaryllis blinked, caught off guard by how serious the question was.*",
            "*She let out a short, breathy laugh, the first in weeks.*",
            '"Battle scenes?"',
        ],
        [],
    ),
    "question_and_exclamation_preserved_through_inversion": (
        'She gasps. "Is that really you?! I cannot believe it!"',
        ["*She smiles, stepping back.* Hello there."],
        ["?!", "it!"],
        [],
    ),
    "mixed_draft_against_quotes_baseline_strips_only_narration_asterisks": (
        '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.',
        ['She smiles. "Hello there."', 'He nods. "Welcome back."'],
        ['"Quite a storm out there,"'],
        ["*"],
    ),
    "mixed_draft_against_asterisk_baseline_strips_only_dialogue_quotes": (
        '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.',
        ["*She smiles, stepping back.* Hello there.", "*He follows her in.* Welcome back."],
        ["*He steps inside, shaking off the rain.*"],
        ['"'],
    ),
    "a_settled_bare_narration_draft_is_still_stripped": (
        '*He steps inside, shaking off the rain.* "Quite a storm out there," he says.',
        BARE_NARRATION_BASELINE,
        [],
        ["*"],
    ),
    "a_first_person_convention_normalizes_a_quoted_reply": (
        'I lean against the desk. "Take a seat, then," I say.',
        [FIRST_PERSON_BEAT],
        ["*I lean against the desk.*"],
        ['"'],
    ),
    "a_talkative_bare_dialogue_baseline_still_sets_the_axes": (
        'Heidi\'s smile is still perfectly in place.\n\n"Ah... Peggy."\n\nShe laughs quietly and airily.\n\n'
        "\"But now that you're here, let's focus on you.\"",
        [HEIDI_GREETING],
        ["*Heidi's smile is still perfectly in place.*", "Ah... Peggy."],
        ['"'],
    ),
}


@pytest.mark.parametrize("draft,base,present,absent", CONTAINS.values(), ids=CONTAINS.keys())
def test_a_conversion_keeps_and_drops_the_right_marks(draft, base, present, absent):
    new = _converted(draft, base)
    assert all(text in new for text in present), new
    assert not any(text in new for text in absent), new


UNCHANGED = {
    "already_consistent": ('He nods slowly. "I understand," he replies.', ['She smiles. "Hello there," she says warmly.']),
    "no_baseline": ('She smiles. "Hello there."', []),
    "unstable_baseline": ('She frowns. "What now?"', [QUOTES_ONLY, ASTERISKS_ONLY]),
    # A scene divider and its blank-line spacing are untouched.
    "scene_divider_run_preserved": ("She turns away.\n\n***\n\nThe room falls silent.", QUOTES_BASELINE),
    "pure_bare_narration_without_dialogue": (
        "She was really nervous about the whole thing.",
        ["*She paces the room nervously, glancing at the clock.* Right."],
    ),
    "drift_in_only_the_latest_turn": (
        'She tilts her head. "What brings you here?"',
        ['She smiles. "Hello there."', 'He nods. "Welcome back."'],
    ),
    "genuine_asterisk_convention_is_not_misread_as_full_markup": (
        "*He shifts his weight.*\n\nAre you sure about this?\n\n*She waits.*",
        [
            "*She smiles, stepping back.* Hello there.",
            "*He follows her in.* Good to see you.",
            "*She turns to face him.* It has been too long.",
        ],
    ),
    # A bare-narration baseline must not unwrap every action beat.
    "an_asterisk_greeting_does_not_strip_the_next_reply": (
        "*stares at you with wide, vacant eyes.*\n\n*aggressive leek spinning*",
        ["miku dayo~ *does a weird dance*"],
    ),
    "single_word_message_is_ambiguous": ("Okay.", GREETING),
    "empty_draft": ("", GREETING),
    "half_asterisked_draft_against_a_bare_narration_baseline": (HALF_ASTERISKED_DRAFT, BARE_NARRATION_BASELINE),
    "half_asterisked_draft_against_a_thought_baseline": (HALF_ASTERISKED_DRAFT, THOUGHT_IN_ASTERISKS_BASELINE),
    "a_thought_in_an_unsettled_draft_survives_a_bare_narration_baseline": (THOUGHT_IN_QUOTED_PROSE, BARE_NARRATION_BASELINE),
    # One unmarked thought in the window does not make the draft's italic one a slip.
    "a_thought_survives_where_the_chat_writes_one_bare": (
        THOUGHT_IN_QUOTED_PROSE,
        [*BARE_NARRATION_BASELINE[:2], 'She looked away. Why do I even bother? "It doesnae matter," she muttered.'],
    ),
    # An unclosed quote swaps speech and narration for the rest of the paragraph.
    "an_unclosed_quote_leaves_its_paragraph_alone": (
        '"Hello, she said, stepping inside. "It\'s cold out there," he replied.',
        FULL_MARKUP_BASELINE,
    ),
    # Two quotes can pair the wrong way round: the span opens onto a space.
    "an_even_count_quote_swap_leaves_its_paragraph_alone": ('She waved. Hello," she said. "Goodbye.', FULL_MARKUP_BASELINE),
}


@pytest.mark.parametrize("draft,base", UNCHANGED.values(), ids=UNCHANGED.keys())
def test_a_draft_is_left_alone(draft, base):
    _assert_unchanged(draft, base)


def test_full_markup_to_quotes_only_strips_narration_asterisks():
    out = normalize_format(FULL_MARKUP, AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.BARE))
    assert "*" not in out.replace("*he murmurs.*", "")  # block narration unwrapped
    assert '"You came back,"' in out  # dialogue untouched


def test_disabled_is_noop():
    assert _assert_unchanged('She smiles. "Hello there."', ["*She smiles.* Hello there."], enabled=False).note == "disabled"


def test_unstable_baseline_votes_unknown_on_both_axes():
    assert baseline_axes([QUOTES_ONLY, ASTERISKS_ONLY]) == AxisStyle(Dialogue.UNKNOWN, Narration.UNKNOWN)


def test_multiparagraph_preserves_separators():
    draft = "She nods slowly.\n\nShe steps away from the table."
    new, rep = normalize_to_baseline(draft, ['*She nods.* "Okay."'], enabled=True)
    assert "\n\n" in new
    if rep.changed:
        assert new.count("\n\n") == draft.count("\n\n")


def test_stable_multiturn_baselines_convert_the_other_convention():
    quoted = [
        'She smiles. "Hello there," she says warmly.',
        'He nods. "Good to see you again," he replies.',
        'She laughs softly. "It has been too long."',
    ]
    assert baseline_axes(quoted) == AxisStyle(Dialogue.QUOTED, Narration.BARE)
    assert "*" not in _converted("*He leans against the doorframe, studying her.* You look tired.", quoted)

    asterisk = [
        "*She smiles, stepping back toward the window.* Hello there.",
        "*He follows, hands in his pockets.* Good to see you.",
        "*She turns to face him fully.* It has been too long.",
    ]
    assert baseline_axes(asterisk) == AxisStyle(Dialogue.BARE, Narration.ASTERISK)
    new = _converted('He leans against the doorframe. "You look tired," he says.', asterisk)
    assert '"' not in new and "*" in new


def test_a_narration_only_draft_is_wrapped_but_unmarked_speech_is_not():
    """No speech plus a bare-narration reading is all narration in a quoting chat.
    Unknown narration may be plain speech, which TTS reads the same way."""
    draft = (
        "The room settles into a heavy silence, broken only by the hum of the TV.\n\n"
        "She rolls onto her back, one arm flung up against the headboard."
    )
    assert classify_axes(draft) == AxisStyle(Dialogue.UNKNOWN, Narration.BARE)
    assert _converted(draft, FULL_MARKUP_BASELINE) == (
        "*The room settles into a heavy silence, broken only by the hum of the TV.*\n\n"
        "*She rolls onto her back, one arm flung up against the headboard.*"
    )
    speech = AxisStyle(Dialogue.UNKNOWN, Narration.UNKNOWN)
    greeting = "Hey, you made it. Come in before the rain starts."
    assert normalize_to_baseline(greeting, FULL_MARKUP_BASELINE, enabled=True, source=speech)[0] == greeting


def test_a_thought_baseline_still_reads_as_bare_narration():
    """Unwrapping converts asterisk narration; a window that sets only thoughts in asterisks is no evidence otherwise."""
    assert baseline_axes(THOUGHT_IN_ASTERISKS_BASELINE).narration == Narration.BARE


# ---------- multiple dialogue beats in one turn ----------


def test_asterisk_narration_without_quotes_classifies_as_bare_dialogue():
    assert classify_axes("*She steps closer.* Are you sure? *She hesitates.* Really sure?").dialogue == Dialogue.BARE


def test_italic_thoughts_in_prose_are_not_read_as_bare_dialogue():
    draft = (
        "Peggy's phone slips from her numb fingers.\n\n*He still likes me. He really does.*\n\n"
        "The thought is not a comfort but an accusation."
    )
    assert classify_axes(draft) == AxisStyle(Dialogue.UNKNOWN, Narration.UNKNOWN)
    _assert_unchanged(draft, GREETING)


def test_the_same_passage_classifies_the_same_in_first_and_third_person():
    """Person and convention are independent."""
    thought = "*He still likes me. He really does.*"
    third = f"Peggy's phone slips from her numb fingers.\n\n{thought}\n\nThe thought is not a comfort."
    first = f"My phone slips from my numb fingers.\n\n{thought}\n\nThe thought is not a comfort."
    assert classify_axes(third) == classify_axes(first)
    assert classify_axes(first).dialogue == Dialogue.UNKNOWN
    for draft in (third, first):
        _assert_unchanged(draft, GREETING)


@pytest.mark.parametrize(
    "draft",
    [
        # Third-person bare runs veto the bare-dialogue reading; the aside keeps its marks.
        "She walked to the window. *Everything had changed.* The street below was empty.",
        # An interior beat is still not an action beat in first person.
        "My phone slips from my numb fingers.\n\n*He still likes me. He really does.*\n\nThe thought is not a comfort.",
    ],
)
def test_an_italic_aside_is_not_bare_dialogue(draft):
    assert classify_axes(draft).dialogue == Dialogue.UNKNOWN
    _assert_unchanged(draft, GREETING)


def test_a_talkative_bare_dialogue_baseline_sets_the_axes():
    assert baseline_axes([HEIDI_GREETING]) == AxisStyle(Dialogue.BARE, Narration.ASTERISK)


@pytest.mark.parametrize(
    "greeting",
    [
        "miku dayo~ *does a weird dance*",
        "Nyaa♪ *pounces on the keyboard*",
        "Tch— *crosses arms*",
        "hi (^_^) *bounces on her heels*",
    ],
)
def test_a_greeting_that_ends_on_stylistic_punctuation_still_sets_the_axes(greeting):
    """A "~" closes a clause, so the beat after it is narration, not italics."""
    assert baseline_axes([greeting]) == AxisStyle(Dialogue.BARE, Narration.ASTERISK)


def test_a_window_that_votes_bare_on_both_axes_enforces_nothing():
    base = [
        "*She smiles and steps back.* I won't go, and you cannot make me.",
        "The rain kept on against the glass, steady and grey, long after she had gone.",
        "He waited by the door for a while, then gave up and went back inside.",
    ]
    assert baseline_axes(base) == AxisStyle(Dialogue.BARE, Narration.BARE)
    _assert_unchanged('*He leans in.* "You came back," he says.', base)


# ---------- the agreement rule, on its own ----------


@pytest.mark.parametrize(
    "samples,expected",
    [
        (["third"], "third"),  # a single confident sample is trusted
        (["ambiguous", "past", "ambiguous"], "past"),  # the unknown sentinel is ignored
        (["ambiguous", "ambiguous"], "ambiguous"),
        ([], "ambiguous"),
        (["third", "third", "first"], "third"),  # a 60% majority...
        (["third", "third", "first", "second"], "ambiguous"),
        (["third", "first", "second"], "ambiguous"),  # ...of at least two, not just a plurality
        (["past", "present"], "ambiguous"),  # an even split
    ],
)
def test_stable_label(samples, expected):
    assert stable_label(samples, "ambiguous") == expected


# ---------- a draft that does not read asterisk keeps its emphasis ----------


def test_a_thought_in_quoted_prose_leaves_narration_unsettled():
    assert classify_axes(THOUGHT_IN_QUOTED_PROSE) == AxisStyle(Dialogue.QUOTED, Narration.UNKNOWN)


@pytest.mark.parametrize(
    "draft",
    [
        "She crossed the room slowly and sat down on the edge of the old bed, smoothing the blanket "
        'flat with both hands. *He was lying again.* "Fine," she said.',
        "The door slammed behind him hard enough to rattle the frames on the wall. *thud* "
        '"Charming," she muttered, and turned the page of her book without looking up.',
        'She stood by the window for a long while, watching the rain run down the glass. *sighs softly* "Fine," she said.',
    ],
)
def test_a_bare_draft_keeps_its_thoughts_and_sound_effects_against_a_bare_baseline(draft):
    """A draft already in bare prose keeps its marks, whether or not the window sets anything in asterisks itself."""
    assert classify_axes(draft).narration == Narration.BARE
    for base in (BARE_NARRATION_BASELINE, THOUGHT_IN_ASTERISKS_BASELINE):
        _assert_unchanged(draft, base)


@pytest.mark.parametrize(
    "draft",
    [
        'Mira pulls the blanket up to his chin. *Aww, poor thing. He must be exhausted.* "Sleep," she whispers.',
        'He reads the note twice, turning it over in his hands. *Really? After all this time?* "Where did you find this?"',
        'The lights flicker once and die, and the hum of the fridge goes with them. *Not again.* "Stay close," she says.',
        '*Such a strange boy,* she thinks, and presses the key into his palm before he can argue. "Don\'t lose it."',
        "He sets the cup down on the saucer without a sound and leans back in his chair, arms folded.\n\n"
        '*He got lucky with that one. Let\'s see how long it lasts.*\n\n"Again," he says, and deals the cards.',
    ],
)
def test_italic_thoughts_keep_their_marks_in_a_chat_that_sets_nothing_in_asterisks(draft):
    """The shapes thoughts take in quoted prose: an interjection, a question, a fragment, a tagged thought, a paragraph."""
    assert skip_reasons(draft) == []
    assert classify_axes(draft).narration != Narration.ASTERISK
    _assert_unchanged(draft, BARE_NARRATION_BASELINE)


# ---------- an explicit source classification ----------


def test_an_explicit_source_replaces_the_draft_classification():
    """Another classifier can decide the source side of the rewrite."""
    base = ['She smiles. "Hello there," she says warmly.']
    draft = "*She steps closer, watching him carefully.* Are you sure about this?"
    # Read as quoted-dialogue prose, its bare run is narration and stays unquoted.
    source = AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.ASTERISK)
    new, rep = normalize_to_baseline(draft, base, enabled=True, source=source)
    assert rep.source is source
    assert new == "She steps closer, watching him carefully. Are you sure about this?"


def test_an_unclosed_quote_is_skipped_even_under_a_correct_reading():
    draft = '"Hello, she said, stepping inside. "It\'s cold out there," he replied.'
    source = AxisStyle(dialogue=Dialogue.QUOTED, narration=Narration.BARE)
    assert normalize_to_baseline(draft, FULL_MARKUP_BASELINE, enabled=True, source=source)[0] == draft


def test_a_quote_inside_a_beat_holds_only_its_paragraph():
    """The parser cuts a quote out of its `*...*` beat, so that paragraph stays as written and the rest converts."""
    beat = '*She narrows her eyes. She doesn\'t want to be "not bothered".*'
    draft = f'{beat}\n\n"Happy now?" *she snaps.* "Or are you scared?"'
    asterisk = [
        "*She smiles, stepping back toward the window.* Hello there.",
        "*He follows, hands in his pockets.* Good to see you.",
        "*She turns to face him fully.* It has been too long.",
    ]
    assert _converted(draft, asterisk) == f"{beat}\n\nHappy now? *she snaps.* Or are you scared?"


# ---------- the action policy (skip-v1): drafts no reading makes safe ----------


@pytest.mark.parametrize(
    "draft,reasons",
    [
        ('Carol: "We have been *thinking*."\nCeline: "Arguing."', ["speaker-label"]),
        ("# Chapter One\n\n*She wakes.*", ["heading"]),
        ("| hp | 10 |\n| mp | 4 |", ["table"]),
        ('*Her throat went dry at the word "wild," her fingers tightening.*', ["quote-in-emphasis"]),
        # Bare speech elsewhere cannot outvote a quote that lives only inside a beat; a quoted line elsewhere can.
        ('It smelled normal.\n\n*She hates the word "normal".*', ["quote-in-emphasis"]),
        ('"Fine."\n\n*She hates the word "fine".*', []),
        # Bullet stars are a list, not a stray asterisk; a bullet opens after any hard line break.
        ("Pack these:\n* rope\n* a lantern", ["list"]),
        ("then * rope", []),
        ("then * rope", ["stray-asterisk"]),
        # Ordinary RP prose is left alone.
        ('*She smiles.* "Hello there," she says.', []),
        ('She crossed the room. *He was lying again.* "Fine," she said.', []),
        ('She said: "Fine." Then she left.', []),
        ("*tilts head* owo", []),
        ('**Chapter One**\n\n*She wakes.* "Morning."', []),
        ('```\nMOOD: happy\n---\n- hp 10\n- mp 4\n```\n*She waves.* "Hi!"', []),  # the fence is never rewritten
    ],
)
def test_skip_reasons(draft, reasons):
    assert skip_reasons(draft) == reasons


def test_skip_reasons_flag_structure_that_is_not_rp_prose():
    assert {"metadata", "rule", "speaker-label"} <= set(
        skip_reasons('REWARD: 13\n\n---\n\nRESPONSE: *NORA smiles.* "Thanks, Oscar!"')
    )
    assert "list" in skip_reasons("*sighs* Fine.\n\nPros:\n- loyal\n- fast")
    assert "stray-asterisk" in skip_reasons("*He should be *furious* by now, but he is not.*")


def test_a_skipped_draft_is_left_as_written_whatever_the_reading():
    base = ['She smiles. "Hello there," she says warmly.']
    draft = "*She steps closer, watching him carefully, one hand on the doorframe.*\n\n---\n\nAre you sure about this?"
    new, rep = normalize_to_baseline(draft, base, enabled=True)
    assert (new, rep.changed, rep.note) == (draft, False, "skipped (rule)")
    assert normalize_format(draft, rep.target) != draft  # the policy held it, not the reading
    # An explicit source (the markup classifier's) is gated the same way.
    source = AxisStyle(dialogue=Dialogue.BARE, narration=Narration.ASTERISK)
    assert normalize_to_baseline(draft, base, enabled=True, source=source)[0] == draft


def test_vote_axes_is_baseline_axes_over_supplied_readings():
    messages = [QUOTES_ONLY, FULL_MARKUP, QUOTES_ONLY, ""]
    assert vote_axes([classify_axes(m) for m in messages]) == baseline_axes(messages)
