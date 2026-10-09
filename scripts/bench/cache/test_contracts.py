"""High-impact observer and qualification contracts for the benchmark harness."""

import json
from pathlib import Path

from scripts.bench.cache.inspect import (
    direction_errors,
    first_prose_text_matches,
    includes_history,
    parse_direction_text,
    prose_defects,
)
from scripts.bench.cache.orb_driver import decode_event, saved_difference, sse_data_line

APPLIED = {
    "moods": [{"id": "grounded", "enabled": 1}, {"id": "tense", "enabled": 1}],
    "fragments": [
        {"id": "keywords", "enabled": 1, "required": 1, "field_type": "array"},
        {"id": "next_event", "enabled": 1, "required": 1, "field_type": "string"},
        {"id": "user_intent", "enabled": 1, "required": 0, "field_type": "string"},
    ],
}


def test_sse_tokens_keep_numeric_text_whitespace_and_native_newlines():
    frames = ["data: Mara", "data:  counted", "data:  ", "data: 3", "data:  records.", "data: \\n\\n", "data:   She paused."]
    draft = "".join(decode_event("token", sse_data_line(line)) for line in frames)
    assert draft == "Mara counted 3 records.\n\n  She paused."
    assert decode_event("writer_done", '{"editor_will_run": true}') == {"editor_will_run": True}


def test_history_requires_exact_role_content_and_order():
    history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]
    assert includes_history({"messages": [{"role": "system", "content": "prefix"}, *history]}, history) == (True, [1, 2])
    assert not includes_history({"messages": list(reversed(history))}, history)[0]
    assert not includes_history({"messages": [history[0], {"role": "assistant", "content": " second"}]}, history)[0]
    hinted = {"role": "assistant", "content": [{"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}]}
    assert includes_history({"messages": [history[0], hinted]}, history)[0]


def test_malformed_native_artifact_is_a_failure_not_a_scoring_crash():
    applied = {
        "moods": [{"id": "grounded", "enabled": 1}],
        "fragments": [{"id": "keywords", "enabled": 1, "required": 1, "field_type": "array"}],
    }
    assert direction_errors({"moods": [{"id": "grounded"}], "keywords": [3]}, applied) == [
        "direction.moods",
        "direction.type.keywords",
    ]
    assert direction_errors({"moods": ["grounded"], "keywords": ["ledger"]}, applied) == []


def test_fixture_rows_and_unique_user_script():
    for path in Path(__file__).parent.joinpath("fixtures").glob("*.json"):
        fixture = json.loads(path.read_text())
        assert len(fixture["user_script"]) == len(set(fixture["user_script"])) == 10
        assert all(row["role"] == ("user" if index % 2 == 0 else "assistant") for index, row in enumerate(fixture["history"]))


def test_native_direction_artifacts_parse_to_the_common_contract():
    text = "**moods**: grounded; tense\n- keywords: harbor records; tide chart, 1920\nnext_event: Mara finds a lead.\n"
    direction = parse_direction_text(text, APPLIED)
    assert direction == {
        "moods": ["grounded", "tense"],
        "keywords": ["harbor records", "tide chart, 1920"],
        "next_event": "Mara finds a lead.",
    }
    assert direction_errors(direction, APPLIED) == []
    assert parse_direction_text("moods:\nkeywords: ledger\nnext_event: She looks up.", APPLIED)["moods"] == []
    assert direction_errors(parse_direction_text("keywords: ledger\nnext_event: x", APPLIED), APPLIED) == ["direction.moods"]
    assert parse_direction_text("Mara looks up.", APPLIED) is None
    assert direction_errors(parse_direction_text("moods: calm\nnext_event: x", APPLIED), APPLIED) == [
        "direction.moods",
        "direction.required.keywords",
    ]


def test_only_native_save_whitespace_is_accepted():
    assert saved_difference("A line.  \nB.\n", "A line.\nB.") == "cleanup"
    assert saved_difference("A.\n\nB.", "A. B.") == "content"
    assert saved_difference("Same.", "Same.") is None


def test_first_prose_must_be_the_reply_start():
    draft = '*Mara* turns the ledger. "Look here," she says.'
    assert first_prose_text_matches("Mara turns the", draft)
    assert not first_prose_text_matches("Working…", draft)
    assert not first_prose_text_matches("", draft)


def test_mechanical_prose_defects():
    assert prose_defects('"Look at the numbers," "Something is off," she said.') == ["prose.adjacent_quotes"]
    assert prose_defects('She said, "They hid what arrived.') == ["prose.unbalanced_quotes"]
    assert prose_defects('"Done."\n\n"Next," she said.') == []
