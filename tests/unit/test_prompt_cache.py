"""Cache breakpoint placement and upstream affinity for chat requests."""

from __future__ import annotations

import copy

from backend.inference.prompt_cache import affinity_headers, mark_cache_breakpoints

LONG = {"type": "ephemeral", "ttl": "1h"}
SHORT = {"type": "ephemeral"}

SYSTEM = {"role": "system", "content": "You narrate."}
HISTORY = [
    {"role": "user", "content": "I open the door."},
    {"role": "assistant", "content": "It creaks."},
]
TAIL = [
    {"role": "user", "content": "(OOC: lore) The door is old."},
    {"role": "user", "content": "I step inside."},
]


def _controls(messages):
    """``(index, cache_control)`` for every marked part, in prompt order."""
    out = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, list):
            out.extend((index, part["cache_control"]) for part in content if "cache_control" in part)
    return out


def test_anchors_sit_at_the_system_base_and_tail_ends():
    messages = [SYSTEM, *HISTORY, *TAIL]
    before = copy.deepcopy(messages)

    marked = mark_cache_breakpoints(messages, prefix_len=3)

    assert _controls(marked) == [(0, LONG), (2, LONG), (4, SHORT)]
    assert marked[2] == {"role": "assistant", "content": [{"type": "text", "text": "It creaks.", "cache_control": LONG}]}
    # Unmarked messages pass through as the same objects; the transcript is untouched.
    assert marked[1] is messages[1] and marked[3] is messages[3]
    assert messages == before


def test_the_next_pass_and_the_next_turn_mark_the_same_base_end():
    """Every call on one base anchors its last message, whatever the tail."""
    director = mark_cache_breakpoints([SYSTEM, *HISTORY, TAIL[1]], prefix_len=3)
    writer = mark_cache_breakpoints([SYSTEM, *HISTORY, *TAIL], prefix_len=3)
    assert director[2] == writer[2]

    grown = [SYSTEM, *HISTORY, TAIL[1], {"role": "assistant", "content": "Dust swirls."}]
    next_turn = mark_cache_breakpoints([*grown, TAIL[0]], prefix_len=len(grown))
    assert _controls(next_turn) == [(0, LONG), (4, LONG), (5, SHORT)]


def test_without_a_base_only_the_system_and_tail_are_marked():
    marked = mark_cache_breakpoints([SYSTEM, *HISTORY, *TAIL], prefix_len=None)
    assert _controls(marked) == [(0, LONG), (4, SHORT)]


def test_anchors_walk_back_past_messages_without_text():
    step = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function"}]}
    messages = [SYSTEM, HISTORY[0], step, {"role": "tool", "tool_call_id": "c1", "content": "ok"}]

    marked = mark_cache_breakpoints(messages, prefix_len=3)

    # The base anchor lands on the user turn before the empty tool-call step.
    assert _controls(marked) == [(0, LONG), (1, LONG), (3, SHORT)]
    assert marked[2] is step


def test_an_anchor_never_crosses_the_previous_one():
    blank = {"role": "user", "content": "   "}
    marked = mark_cache_breakpoints([SYSTEM, HISTORY[0], blank], prefix_len=2)
    # The tail has no text of its own, so it is left unmarked rather than
    # stacking a second marker onto the base anchor.
    assert _controls(marked) == [(0, LONG), (1, LONG)]


def test_multimodal_messages_mark_their_last_text_part():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    message = {"role": "user", "content": [{"type": "text", "text": "Look."}, image]}

    marked = mark_cache_breakpoints([SYSTEM, message], prefix_len=None)

    assert marked[1]["content"] == [{"type": "text", "text": "Look.", "cache_control": SHORT}, image]
    assert "cache_control" not in message["content"][0]


def test_longer_ttls_always_precede_shorter_ones():
    for prefix_len in (None, 0, 1, 2, 3, 4, 5, 9):
        ttls = [control.get("ttl") for _, control in _controls(mark_cache_breakpoints([SYSTEM, *HISTORY, *TAIL], prefix_len))]
        assert ttls == sorted(ttls, key=lambda ttl: ttl != "1h")


def test_no_system_prompt_and_out_of_range_bases_degrade_cleanly():
    assert _controls(mark_cache_breakpoints([*HISTORY], prefix_len=0)) == [(1, SHORT)]
    assert _controls(mark_cache_breakpoints([SYSTEM, *HISTORY], prefix_len=9)) == [(0, LONG), (2, LONG)]
    assert mark_cache_breakpoints([], prefix_len=3) == []


def test_affinity_is_one_id_per_lane():
    director = affinity_headers("m", [SYSTEM, *HISTORY, TAIL[1]])
    writer = affinity_headers("m", [SYSTEM, *HISTORY, *TAIL])
    assert director == writer
    assert set(director) == {"x-session-id"}
    assert affinity_headers("other", [SYSTEM]) != director
    assert affinity_headers("m", [{"role": "system", "content": "Another card."}]) != director
    assert affinity_headers("m", []) == {}
