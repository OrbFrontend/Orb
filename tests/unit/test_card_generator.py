"""The generator's output contract and one-shot model request."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from backend.features.card_generator import deep
from backend.features.card_generator.generator import (
    FIELD_CAPS,
    GENERATE_CARD_TOOL,
    CardGenerationUnavailable,
    clean_card,
    generate_card,
)
from backend.inference import LLMClient
from backend.inference.schema import strictify_schema

DRAFT = {"name": "Mara", "first_mes": "Hello, {{user}}.\nCome inside."}


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", None),
        ("name", " \n"),
        ("name", 42),
        ("name", "x" * 101),
        ("first_mes", None),
        ("first_mes", " "),
        ("first_mes", 42),
        ("description", "{{roll::1d6}}"),
        ("scenario", "{broken}"),
        ("name", "{{char}}"),
        ("creator_notes", "{{user}}"),
    ],
)
def test_unrecoverable_fields_fail(field, value):
    with pytest.raises(CardGenerationUnavailable):
        clean_card({**DRAFT, field: value})


@pytest.mark.parametrize("field,cap", FIELD_CAPS.items())
def test_long_prose_clamps_at_last_complete_sentence(field, cap):
    sentence = "A complete sentence. "
    prefix = sentence * (cap // len(sentence))
    result = clean_card({**DRAFT, field: prefix + "Long continuation " * cap})
    assert result[field] == prefix.rstrip()
    assert len(result[field]) <= cap


@pytest.mark.parametrize("field", [key for key in FIELD_CAPS if key != "creator_notes"])
def test_allowed_macros_and_paragraphs_survive(field):
    prose = "{{CHAR}} welcomes {{user}}.\n\nA new paragraph."
    assert clean_card({**DRAFT, field: prose})[field] == prose


def test_soft_fields_coerce_missing_or_null_to_empty_and_strip_extra_keys():
    card = clean_card({**DRAFT, "mes_example": None, "description": 5, "id": "foreign", "system_prompt": "override"})
    assert card["mes_example"] == card["description"] == card["creator_notes"] == ""
    assert card["source_format"] == "generated"
    assert card["tags"] == card["alternate_greetings"] == []
    assert "id" not in card and "system_prompt" not in card


def test_braces_past_the_clamp_still_fail():
    with pytest.raises(CardGenerationUnavailable):
        clean_card({**DRAFT, "description": "Safe. " * 2000 + "{{date}}"})


def test_clamping_falls_back_to_words_then_hard_cut_without_splitting_macros():
    assert clean_card({**DRAFT, "scenario": "word " * 400})["scenario"] == ("word " * 320).rstrip()
    assert clean_card({**DRAFT, "scenario": "x" * 1700})["scenario"] == "x" * 1600
    text = "x" * 1597 + "{{char}}" + "x" * 100
    assert clean_card({**DRAFT, "scenario": text})["scenario"] == "x" * 1597


def test_all_seven_fields_remain_required_nonnullable_in_strict_schema():
    schema = strictify_schema(GENERATE_CARD_TOOL["function"]["parameters"])
    assert schema["required"] == ["name", *FIELD_CAPS]
    assert all(prop["type"] == "string" for prop in schema["properties"].values())


@pytest.mark.parametrize("thinking,budget,expected", [(False, 200, 4096), (True, 200, 8192), (False, 16000, 16000)])
async def test_budget_and_thinking_are_explicit(thinking, budget, expected):
    captured = {}

    async def complete(**kwargs):
        captured.update(kwargs)
        yield {
            "type": "done",
            "message": {
                "tool_calls": [
                    {
                        "id": "draft",
                        "type": "function",
                        "function": {
                            "name": "generate_character_card",
                            "arguments": json.dumps(DRAFT),
                        },
                    }
                ]
            },
        }

    client = Mock(spec=LLMClient)
    client.complete = complete
    result = await generate_card(
        client,
        "agent",
        'idea """ boundary',
        settings={"agent_max_tokens": budget},
        reasoning_on=thinking,
        library_digest='names """ data',
    )
    assert result["name"] == "Mara"
    assert captured["max_tokens"] == expected
    assert captured["chat_template_kwargs"]["enable_thinking"] is thinking
    assert captured["messages"][1]["content"].count('"""') == 4


async def test_no_tool_call_is_an_unavailable_draft():
    async def complete(**kwargs):
        yield {"type": "done", "message": {"content": "Here is your card!"}}

    client = Mock(spec=LLMClient)
    client.complete = complete
    with pytest.raises(CardGenerationUnavailable):
        await generate_card(client, "agent", "idea", settings={})


def test_generator_caps_fit_the_editor_limits():
    source = (Path(__file__).parents[2] / "frontend" / "validate.js").read_text()
    import re

    limits = {key: int(value) for key, value in re.findall(r"const (MAX_CHARACTER_\w+) = (\d+);", source)}
    assert 100 <= limits["MAX_CHARACTER_NAME"]
    assert max(FIELD_CAPS.values()) <= limits["MAX_CHARACTER_FIELD"]
    assert FIELD_CAPS["creator_notes"] <= limits["MAX_CHARACTER_ADVANCED"]


# --- Deep tailoring: the research loop ---------------------------------------

CARD = {**DRAFT, "description": "A fence.", "personality": "", "scenario": "", "mes_example": "", "creator_notes": ""}


def _reply(name, args, reasoning=""):
    call = {"id": "call_0", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"content": "", "tool_calls": [call], **({"reasoning_content": reasoning} if reasoning else {})}


def _step(sql="SELECT name FROM characters LIMIT 5", *, findings="", purpose="Favourite characters", finished=False):
    return _reply("query_library", {"findings": findings, "purpose": purpose, "sql": sql, "finished": finished})


def _card(**overrides):
    return _reply("generate_character_card", {**CARD, **overrides})


def _provider_error():
    request = httpx.Request("POST", "https://provider.invalid")
    return httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))


class ScriptedClient:
    """Replays one scripted ``done`` message (or raises one error) per call."""

    base_url = ""

    def __init__(self, *script, abort_after=None):
        self.script = list(script)
        self.calls = []
        self.is_aborted = False
        self.abort_after = abort_after

    async def complete(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == self.abort_after:
            self.is_aborted = True
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        yield {"type": "done", "message": item}


@pytest.fixture
def queries(monkeypatch):
    ran = []

    async def run(sql, **caps):
        ran.append((sql, caps))
        return (
            {"error": "no such column: nope"}
            if "nope" in sql
            else {"columns": ["name"], "rows": [["Mara"]], "more_rows": False}
        )

    monkeypatch.setattr(deep, "run_library_query", run)
    return ran


async def _events(client, **settings):
    return [event async for event in deep.generate_deep_card(client, "agent", "A fence", settings=settings, digest="{}")]


def _forced(call):
    return call["tool_choice"]["function"]["name"]


def _labels(events):
    return [event["label"] for event in events if event["type"] == "progress"]


def _assert_append_only(calls):
    for before, after in zip(calls, calls[1:]):
        assert after["messages"][: len(before["messages"])] == before["messages"]


async def test_deep_research_finishes_then_drafts_over_one_growing_transcript(queries):
    client = ScriptedClient(
        _step(purpose="  Most   played\ncharacters. ", findings=""),
        _step("SELECT nope FROM characters", findings="Likes noir.", purpose="Tag mix"),
        _step("", findings="Likes noir harbours.", finished=True),
        _card(),
    )
    events = await _events(client)
    assert events[-1] == {"type": "done", "card": deep.clean_card(CARD)}
    assert _labels(events) == [
        "Researching your library: Most played characters (step 1 of 10)…",
        "Researching your library: Tag mix (step 2 of 10)…",
        "Drafting your character…",
    ]
    assert [_forced(call) for call in client.calls] == ["query_library"] * 3 + ["generate_character_card"]
    assert len(queries) == 2 and queries[0][1] == {
        "max_rows": 50,
        "max_cell_chars": 500,
        "max_result_chars": 4000,
        "time_limit_s": 5.0,
    }
    _assert_append_only(client.calls)
    blob = json.dumps(deep.TOOLS)
    assert all(json.dumps(call["tools"]) == blob for call in client.calls)
    assert all(call["chat_template_kwargs"]["enable_thinking"] is True for call in client.calls)
    assert all(call["max_tokens"] == 8192 and call["temperature"] == 0.2 for call in client.calls)

    final = client.calls[-1]["messages"]
    assert final[0]["content"] == deep.DEEP_SYSTEM_PROMPT
    assert deep.VIEW_DOCS in final[1]["content"] and deep.STEP_PROTOCOL in final[1]["content"]
    replayed = [m for m in final if m["role"] == "assistant"]
    results = [m for m in final if m["role"] == "tool"]
    assert [m["tool_calls"][0]["id"] for m in replayed] == [m["tool_call_id"] for m in results] == ["step1", "step2", "step3"]
    assert json.loads(replayed[0]["tool_calls"][0]["function"]["arguments"])["purpose"] == "Most   played\ncharacters."
    assert json.loads(results[0]["content"]) == {"steps_left": 9, "columns": ["name"], "rows": [["Mara"]], "more_rows": False}
    assert json.loads(results[1]["content"]) == {"steps_left": 8, "error": "no such column: nope"}
    assert results[2]["content"] == deep.DRAFT_NOTE


async def test_deep_research_budget_ends_with_the_draft_note(queries):
    client = ScriptedClient(*[_step(f"SELECT {i}") for i in range(deep.MAX_STEPS)], _card())
    events = await _events(client)
    assert events[-1]["type"] == "done"
    assert _labels(events)[-2] == f"Researching your library: Favourite characters (step {deep.MAX_STEPS} of {deep.MAX_STEPS})…"
    last = client.calls[-1]["messages"][-1]
    head, note = last["content"].split("\n\n", 1)
    assert json.loads(head)["steps_left"] == 0 and note == deep.DRAFT_NOTE
    assert len(queries) == deep.MAX_STEPS


async def test_reasoning_is_replayed_only_when_the_step_had_some(queries):
    thought = _reply("query_library", {"findings": "", "purpose": "p", "sql": "SELECT 1", "finished": False}, "Hmm.")
    client = ScriptedClient(thought, _step(sql="", finished=True), _card())
    await _events(client)
    assert client.calls[1]["messages"][2]["reasoning_content"] == "Hmm."
    assert "reasoning_content" not in client.calls[2]["messages"][4]


async def test_reasoning_is_replayed_under_the_field_names_the_provider_streamed(queries):
    details = [{"type": "reasoning.text", "text": "Hmm.", "signature": "sig", "index": 0}]
    thought = {**_step(), "reasoning": "Hmm.", "reasoning_details": details}
    client = ScriptedClient(thought, _step(sql="", finished=True), _card())
    await _events(client)
    replayed = client.calls[1]["messages"][2]
    assert (replayed["reasoning"], replayed["reasoning_details"]) == ("Hmm.", details)
    assert "reasoning_content" not in replayed


@pytest.mark.parametrize("reply", [_step(""), _step("   ")])
async def test_a_step_without_sql_ends_research(queries, reply):
    client = ScriptedClient(_step(), reply, _card())
    events = await _events(client)
    assert events[-1]["type"] == "done" and len(queries) == 1
    assert [_forced(call) for call in client.calls] == ["query_library"] * 2 + ["generate_character_card"]
    last = client.calls[-1]["messages"][-1]
    assert (last["role"], last["content"]) == ("tool", deep.DRAFT_NOTE)


@pytest.mark.parametrize("reply", [_card(), {"content": "I would rather just write the card."}, {}])
async def test_a_reply_without_a_query_ends_research_on_the_last_result(queries, reply):
    client = ScriptedClient(_step(), reply, _card())
    events = await _events(client)
    assert events[-1]["type"] == "done" and len(queries) == 1
    assert [_forced(call) for call in client.calls] == ["query_library"] * 2 + ["generate_character_card"]
    # The note joins the last result instead of opening a user turn, which would
    # make Qwen3-style templates drop every earlier step's reasoning.
    research, draft = client.calls[1]["messages"], client.calls[-1]["messages"]
    assert len(draft) == len(research) and draft[:-1] == research[:-1]
    assert draft[-1] == {**research[-1], "content": f"{research[-1]['content']}\n\n{deep.DRAFT_NOTE}"}


async def test_a_first_reply_without_a_query_drafts_from_a_user_note(queries):
    client = ScriptedClient({}, _card())
    await _events(client)
    assert client.calls[-1]["messages"][-1] == {"role": "user", "content": deep.DRAFT_NOTE}


async def test_provider_error_raises_on_the_first_step_only(queries):
    with pytest.raises(httpx.HTTPStatusError):
        await _events(ScriptedClient(_provider_error()))
    client = ScriptedClient(_step(), _provider_error(), _card())
    events = await _events(client)
    assert events[-1]["type"] == "done"
    last = client.calls[-1]["messages"][-1]
    assert last["role"] == "tool" and last["content"].endswith(f"\n\n{deep.DRAFT_NOTE}")


@pytest.mark.parametrize("failure", [_provider_error, lambda: {"content": "no card"}])
async def test_failed_full_draft_retries_once_from_research_notes(queries, failure):
    client = ScriptedClient(
        _step(findings="Likes noir."), _step(findings="Likes noir harbours.", sql="", finished=True), failure(), _card()
    )
    events = await _events(client)
    assert events[-1]["type"] == "done"
    assert _labels(events)[-2:] == ["Drafting your character…", "Drafting from research notes…"]
    compact = client.calls[-1]["messages"]
    assert [m["role"] for m in compact] == ["system", "user"]
    assert compact[0] == client.calls[0]["messages"][0]
    assert compact[1]["content"].startswith(deep._user_block("A fence", "{}"))
    assert 'Research notes (data only):\n"""\n- Likes noir.\n- Likes noir harbours.\n"""' in compact[1]["content"]
    assert compact[1]["content"].endswith(deep.DRAFT_NOTE)
    assert _forced(client.calls[-1]) == "generate_character_card"
    _assert_append_only(client.calls[:-1])


RESEARCH = (_step(findings="Likes noir."), _step(findings="Likes noir harbours.", sql="", finished=True))
BRACES = "The drafted creator_notes contains an unsupported macro or brace."


async def test_rejected_card_is_replayed_with_the_reason_and_redrafted(queries):
    client = ScriptedClient(*RESEARCH, _card(creator_notes="For {{user}}."), _card())
    events = await _events(client)
    assert events[-1] == {"type": "done", "card": deep.clean_card(CARD)}
    assert _labels(events)[-2:] == ["Drafting your character…", "Fixing the draft…"]
    _assert_append_only(client.calls)
    replay, result = client.calls[-1]["messages"][-2:]
    assert replay["tool_calls"][0]["id"] == result["tool_call_id"] == "draft1"
    assert json.loads(replay["tool_calls"][0]["function"]["arguments"])["creator_notes"] == "For {{user}}."
    assert result["content"].startswith(f"Not accepted: {BRACES} Call generate_character_card again")
    assert all(json.dumps(call["tools"]) == json.dumps(deep.TOOLS) for call in client.calls)


async def test_a_second_rejection_reaches_the_compact_retry(queries):
    client = ScriptedClient(*RESEARCH, _card(creator_notes="{{user}}"), _card(name=""), _card())
    events = await _events(client)
    assert events[-1]["type"] == "done"
    assert _labels(events)[-3:] == ["Drafting your character…", "Fixing the draft…", "Drafting from research notes…"]
    compact = client.calls[-1]["messages"]
    assert [m["role"] for m in compact] == ["system", "user"]
    assert compact[1]["content"].endswith(
        f"An earlier draft was not accepted: The model returned no name for the card.\n\n{deep.DRAFT_NOTE}"
    )


async def test_a_card_rejected_in_the_compact_retry_is_corrected_there(queries):
    client = ScriptedClient(*RESEARCH, _provider_error(), _card(creator_notes="{{user}}"), _card())
    events = await _events(client)
    assert events[-1]["type"] == "done"
    assert _labels(events)[-3:] == ["Drafting your character…", "Drafting from research notes…", "Fixing the draft…"]
    fixed = client.calls[-1]["messages"]
    assert [m["role"] for m in fixed] == ["system", "user", "assistant", "tool"]
    assert fixed[:2] == client.calls[-2]["messages"]
    assert "An earlier draft was not accepted" not in fixed[1]["content"]
    assert fixed[-1]["content"].startswith(f"Not accepted: {BRACES}")


async def test_drafting_stops_after_three_attempts(queries):
    client = ScriptedClient(*RESEARCH, _card(name=""), _card(name=""), _card(name=""), _card())
    with pytest.raises(CardGenerationUnavailable, match="no name"):
        await _events(client)
    assert [_forced(call) for call in client.calls].count("generate_character_card") == 3


async def test_a_rejected_card_is_corrected_even_when_no_query_ran(queries):
    client = ScriptedClient(_step(sql="", finished=True), _card(name=""), _card())
    assert (await _events(client))[-1]["type"] == "done"
    client = ScriptedClient(_step(sql="", finished=True), _card(name=""), _card(name=""), _card())
    with pytest.raises(CardGenerationUnavailable):
        await _events(client)
    assert len(client.calls) == 3


async def test_failed_compact_retry_raises(queries):
    client = ScriptedClient(_step(), _step(sql="", finished=True), _provider_error(), {"content": "still no card"})
    with pytest.raises(CardGenerationUnavailable):
        await _events(client)
    assert len(client.calls) == 4


async def test_no_retry_when_no_query_ran(queries):
    client = ScriptedClient(_step(sql="", finished=True), _provider_error())
    with pytest.raises(httpx.HTTPStatusError):
        await _events(client)
    assert len(client.calls) == 2


async def test_abort_between_steps_stops_quietly(queries):
    client = ScriptedClient(_step(), _step(), abort_after=1)
    assert await _events(client) == []
    assert len(client.calls) == 1 and queries == []
    client = ScriptedClient(_step(sql="", finished=True), _provider_error(), abort_after=2)
    assert _labels(await _events(client)) == ["Drafting your character…"]


async def test_endpoint_ignoring_forced_choice_gets_only_the_forced_tool(queries, monkeypatch):
    monkeypatch.setattr(deep, "honors_forced_tool_choice", lambda *args: False)
    client = ScriptedClient(_step(), _step(sql="", finished=True), _card())
    await _events(client, agent_max_tokens=16000)
    assert [call["tools"] for call in client.calls] == [[deep.QUERY_TOOL], [deep.QUERY_TOOL], [GENERATE_CARD_TOOL]]
    assert client.calls[0]["max_tokens"] == 16000


def test_purpose_labels_are_one_line_and_cut_at_a_word():
    assert deep._purpose_label("") == ""
    long = "word " * 40
    label = deep._purpose_label(long)
    assert label.endswith("word…") and len(label) <= 121
    assert deep._purpose_label("x" * 200) == "x" * 120 + "…"


# --- Single drafts: one corrective redraft -----------------------------------


async def test_single_draft_rejection_is_shown_to_the_model_and_redrafted_once():
    client = ScriptedClient(_card(creator_notes="For {{user}}."), _card())
    card = await generate_card(client, "agent", "A fence", settings={}, reasoning_on=True, library_digest="{}")
    assert card == deep.clean_card(CARD)
    first, second = client.calls
    assert first["messages"] == second["messages"][:2]
    replay, result = second["messages"][2:]
    assert replay["tool_calls"][0]["id"] == result["tool_call_id"] == "draft1"
    assert json.loads(replay["tool_calls"][0]["function"]["arguments"])["creator_notes"] == "For {{user}}."
    assert result["content"].startswith(f"Not accepted: {BRACES} Call generate_character_card again")
    assert first["tools"] == second["tools"] == [GENERATE_CARD_TOOL]
    assert first["chat_template_kwargs"] == second["chat_template_kwargs"]
    assert first["max_tokens"] == second["max_tokens"] == 8192


async def test_single_draft_gives_up_after_the_redraft():
    client = ScriptedClient(_card(name=""), _card(creator_notes="{{char}}"), _card())
    with pytest.raises(CardGenerationUnavailable, match="creator_notes"):
        await generate_card(client, "agent", "A fence", settings={})
    assert len(client.calls) == 2


async def test_single_draft_without_a_card_or_after_abort_is_not_redrafted():
    client = ScriptedClient({"content": "Here is your card!"}, _card())
    with pytest.raises(CardGenerationUnavailable, match="did not return"):
        await generate_card(client, "agent", "A fence", settings={})
    client = ScriptedClient(_card(name=""), _card(), abort_after=1)
    with pytest.raises(CardGenerationUnavailable, match="no name"):
        await generate_card(client, "agent", "A fence", settings={})
    assert len(client.calls) == 1
