from backend.inference.client import parse_tool_calls

OPEN = "<|tool_call>"
CLOSE = "<tool_call|>"
Q = '<|"|>'


def test_gemma_native_through_parse_tool_calls():
    content = OPEN + "call:direct_scene{moods:[" + Q + "talkative" + Q + "],history-summary:" + Q + "x, y" + Q + "}" + CLOSE
    assert parse_tool_calls({"content": content}) == [
        {"name": "direct_scene", "arguments": {"moods": ["talkative"], "history-summary": "x, y"}}
    ]


def test_standard_tool_calls():
    msg = {"tool_calls": [{"function": {"name": "x", "arguments": '{"a": 1}'}}]}
    assert parse_tool_calls(msg) == [{"name": "x", "arguments": {"a": 1}}]


def test_hermes_tags():
    msg = {"content": '<tool_call>{"name": "x", "arguments": {"a": 1}}</tool_call>'}
    assert parse_tool_calls(msg) == [{"name": "x", "arguments": {"a": 1}}]


def test_json_in_content():
    msg = {"content": '{"name": "x", "arguments": {}}'}
    assert parse_tool_calls(msg) == [{"name": "x", "arguments": {}}]


def test_sanitize_strips_leaked_delimiter():
    # A server that parsed the DSL to JSON but left the <|"|> token inside a
    # string value: arguments decodes to {"k": 'a<|"|>b'}, sanitized to 'ab'.
    msg = {"tool_calls": [{"function": {"name": "x", "arguments": r'{"k":"a<|\"|>b"}'}}]}
    assert parse_tool_calls(msg) == [{"name": "x", "arguments": {"k": "ab"}}]


def test_empty_message():
    assert parse_tool_calls({"content": ""}) == []
    assert parse_tool_calls({}) == []


def test_editor_apply_patch_objects():
    content = OPEN + "call:editor_apply_patch{patches:[{search:" + Q + "foo" + Q + ",replace:" + Q + "bar" + Q + "}]}" + CLOSE
    assert parse_tool_calls({"content": content}) == [
        {"name": "editor_apply_patch", "arguments": {"patches": [{"search": "foo", "replace": "bar"}]}}
    ]


# ── argument decoding: salvage, and the shapes that must not reach a pass ─────


def test_arguments_salvaged_from_a_markup_wrapper():
    # A route that ignores its forced schema often still emits the right
    # object, merely wrapped -- observed as <memo> from a NanoGPT stealth model.
    wrapped = '<memo lang="en"><small>{"moods": ["tense"]}</small></memo>'
    msg = {"tool_calls": [{"function": {"name": "direct_scene", "arguments": wrapped}}]}
    assert parse_tool_calls(msg) == [{"name": "direct_scene", "arguments": {"moods": ["tense"]}}]


def test_arguments_salvaged_from_a_code_fence():
    msg = {"tool_calls": [{"function": {"name": "x", "arguments": '```json\n{"a": 1}\n```'}}]}
    assert parse_tool_calls(msg) == [{"name": "x", "arguments": {"a": 1}}]


def test_salvage_is_string_aware_about_braces_in_prose():
    # direct_scene's fields are prose; a brace inside a string value must not
    # close the object early, and a brace inside a *quoted* one must not open it.
    args = '<memo>{"problem": "the {{char}} stalls", "next_event": "a closing brace: }"}</memo>'
    msg = {"tool_calls": [{"function": {"name": "direct_scene", "arguments": args}}]}
    assert parse_tool_calls(msg) == [
        {"name": "direct_scene", "arguments": {"problem": "the {{char}} stalls", "next_event": "a closing brace: }"}}
    ]


def test_prose_with_no_object_still_degrades_to_empty_args():
    args = "<memo><small>- moods: [talkative, grounded]</small></memo>"
    msg = {"tool_calls": [{"function": {"name": "direct_scene", "arguments": args}}]}
    assert parse_tool_calls(msg) == [{"name": "direct_scene", "arguments": {}}]


def test_truncated_arguments_degrade_rather_than_salvage_a_fragment():
    msg = {"tool_calls": [{"function": {"name": "x", "arguments": '{"a": 1, "b": {"c"'}}]}
    assert parse_tool_calls(msg) == [{"name": "x", "arguments": {}}]


def test_non_object_arguments_never_reach_the_caller():
    # Valid JSON, wrong shape. Passes index into `arguments`; a str or list
    # would raise inside the pass instead of reading as "the model skipped".
    for raw in ('"just a string"', "[1, 2]", "42", "null"):
        msg = {"tool_calls": [{"function": {"name": "x", "arguments": raw}}]}
        assert parse_tool_calls(msg) == [{"name": "x", "arguments": {}}], raw
