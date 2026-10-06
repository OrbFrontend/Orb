from backend.inference.gemma_tool_format import parse_gemma_tool_calls

OPEN = "<|tool_call>"
CLOSE = "<tool_call|>"
Q = '<|"|>'  # Gemma's symmetric string-delimiter token

GOLDEN = (
    OPEN
    + "call:direct_scene{detected_repetitions:[],history-summary:"
    + Q
    + "Aqua was recruited by Anon for a high-level quest. She signed a binding contract "
    "with the Eldritch Defense Guild, was dressed in ritual gear (including a plague "
    "doctor mask), and was informed of a mission involving an ancient evil god in the "
    "sewers. She has now been transported to a basement where a cage elevator awaits them."
    + Q
    + ",keywords:["
    + Q
    + "Eldritch Defense Guild"
    + Q
    + ","
    + Q
    + "cognitohazard"
    + Q
    + ","
    + Q
    + "sacrificial grounds"
    + Q
    + ","
    + Q
    + "void elevator"
    + Q
    + "],moods:["
    + Q
    + "talkative"
    + Q
    + ","
    + Q
    + "tense"
    + Q
    + "],relevant-history:"
    + Q
    + "Anon's refusal to share details -> Signing the contract -> Masking for cognitohazard "
    "protection -> Reveal of the mission -> Teleportation to the void elevator." + Q + "}" + CLOSE
)

GOLDEN_ARGS = {
    "detected_repetitions": [],
    "history-summary": (
        "Aqua was recruited by Anon for a high-level quest. She signed a binding contract "
        "with the Eldritch Defense Guild, was dressed in ritual gear (including a plague "
        "doctor mask), and was informed of a mission involving an ancient evil god in the "
        "sewers. She has now been transported to a basement where a cage elevator awaits them."
    ),
    "keywords": ["Eldritch Defense Guild", "cognitohazard", "sacrificial grounds", "void elevator"],
    "moods": ["talkative", "tense"],
    "relevant-history": (
        "Anon's refusal to share details -> Signing the contract -> Masking for cognitohazard "
        "protection -> Reveal of the mission -> Teleportation to the void elevator."
    ),
}


def test_golden_sample():
    assert parse_gemma_tool_calls(GOLDEN) == [{"name": "direct_scene", "arguments": GOLDEN_ARGS}]


def test_empty_array():
    content = OPEN + "call:direct_scene{moods:[]}" + CLOSE
    assert parse_gemma_tool_calls(content) == [{"name": "direct_scene", "arguments": {"moods": []}}]


def test_hyphen_key():
    content = OPEN + "call:direct_scene{history-summary:" + Q + "x" + Q + "}" + CLOSE
    assert parse_gemma_tool_calls(content) == [{"name": "direct_scene", "arguments": {"history-summary": "x"}}]


def test_comma_inside_string_not_split():
    content = OPEN + "call:t{history-summary:" + Q + "a, b, c" + Q + "}" + CLOSE
    assert parse_gemma_tool_calls(content)[0]["arguments"] == {"history-summary": "a, b, c"}


def test_bracket_inside_string():
    content = OPEN + "call:t{summary:" + Q + "see [ref] here" + Q + "}" + CLOSE
    assert parse_gemma_tool_calls(content)[0]["arguments"] == {"summary": "see [ref] here"}


def test_brace_inside_string():
    content = OPEN + "call:t{summary:" + Q + "code {x} end" + Q + "}" + CLOSE
    assert parse_gemma_tool_calls(content)[0]["arguments"] == {"summary": "code {x} end"}


def test_multiple_concatenated_calls():
    calls = parse_gemma_tool_calls(OPEN + "call:a{moods:[" + Q + "x" + Q + "]}" + CLOSE + OPEN + "call:b{keywords:[]}" + CLOSE)
    assert [c["name"] for c in calls] == ["a", "b"]
    assert calls[0]["arguments"] == {"moods": ["x"]}
    assert calls[1]["arguments"] == {"keywords": []}


def test_bare_scalars():
    content = OPEN + "call:t{n:5,f:1.5,b:true,c:false}" + CLOSE
    assert parse_gemma_tool_calls(content)[0]["arguments"] == {"n": 5, "f": 1.5, "b": True, "c": False}


def test_string_array():
    content = OPEN + "call:t{k:[" + Q + "a" + Q + "," + Q + "b" + Q + "]}" + CLOSE
    assert parse_gemma_tool_calls(content)[0]["arguments"] == {"k": ["a", "b"]}


def test_no_call_returns_empty():
    assert parse_gemma_tool_calls("She smiled. The end.") == []


def test_truncated_call_dropped():
    assert parse_gemma_tool_calls(OPEN + "call:t{moods:[" + Q + "a" + Q + "]") == []


def test_unterminated_string_best_effort():
    assert parse_gemma_tool_calls(OPEN + "call:t{k:" + Q + "abc}" + CLOSE)[0]["arguments"] == {"k": "abc}"}


def test_object_array_element():
    content = OPEN + "call:editor_apply_patch{patches:[{search:" + Q + "foo" + Q + ",replace:" + Q + "bar" + Q + "}]}" + CLOSE
    assert parse_gemma_tool_calls(content) == [
        {"name": "editor_apply_patch", "arguments": {"patches": [{"search": "foo", "replace": "bar"}]}}
    ]
