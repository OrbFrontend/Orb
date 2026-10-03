"""Enabling or disabling a fragment never rewrites the shared tools blob.

The blob renders ahead of the conversation in the cached prefix, so the
fragment-built tools offer every defined fragment and each call narrows to its
live fields per call (see "Treat tools as part of the prompt" in
docs/architecture/kv-cache.md).
"""

from __future__ import annotations

import json

from backend.core import RESERVED_FRAGMENT_IDS
from backend.inference import CachedBase
from backend.pipeline.config import build_writer_tools_blob
from backend.pipeline.context import _defined_fragments
from backend.pipeline.passes._prompting import tool_call_instruction
from backend.pipeline.passes.director.director import (
    SPEAKING_PLAN_FIELD,
    SPEAKING_PLAN_SCHEMA_DESCRIPTION,
    director_pass,
    live_direct_scene_schema,
)
from backend.pipeline.passes.editor.feedback import feedback_step
from backend.pipeline.passes.state import offered_state_ids
from backend.prompting.tool_catalog import enabled_schemas


def _row(fid: str, field_type: str, sort_order: int, *, enabled: int = 1, required: int = 0, **extra) -> dict:
    return {
        "id": fid,
        "label": fid,
        "injection_label": fid.title(),
        "description": f"Instruction for {fid}",
        "field_type": field_type,
        "required": required,
        "sort_order": sort_order,
        "enabled": enabled,
        **extra,
    }


_GLOBALS = [
    _row("intent", "string", 1, required=1),
    _row("next_event", "string", 2, required=1),
    _row("keywords", "array", 3),
    _row("suggestions", "feedback", 4),
    _row("humanize", "post_processing", 5),
    _row("mood_note", "state", 6, state_mode="value", state_update="before_writer"),
    _row("facts", "state", 7, state_mode="entries", state_update="after_reply"),
]
_SETTINGS = {"enable_agent": 1}


def _toggled(off: set[str]) -> list[dict]:
    return [{**row, "enabled": 0 if row["id"] in off else 1} for row in _GLOBALS]


def _blob(globals_: list[dict], card_rows: list[dict] | None = None) -> tuple[str, dict[str, bool]]:
    overrides, enabled = build_writer_tools_blob(
        _SETTINGS, _defined_fragments(globals_, card_rows or []), {"direct_scene": True}
    )
    return json.dumps(enabled_schemas(enabled, overrides)), enabled


class TestBlobSurvivesToggles:
    def test_every_single_toggle_leaves_the_blob_byte_identical(self):
        reference, _ = _blob(_toggled(set()))
        for row in _GLOBALS:
            assert _blob(_toggled({row["id"]}))[0] == reference, row["id"]

    def test_disabling_every_fragment_of_a_kind_keeps_its_tool(self):
        reference, _ = _blob(_toggled(set()))
        blob, enabled = _blob(_toggled({"suggestions", "humanize", "facts"}))
        assert blob == reference
        assert enabled["give_feedback"] and enabled["editor_search_replace"] and enabled["update_state"]

    def test_disabled_fragments_are_offered_with_nothing_required(self):
        schemas = {s["function"]["name"]: s for s in json.loads(_blob(_toggled({"intent", "suggestions"}))[0])}
        direct_scene = schemas["direct_scene"]["function"]["parameters"]
        assert list(direct_scene["properties"]) == ["intent", "next_event", "keywords", "mood_note", "moods"]
        assert direct_scene["required"] == []
        assert schemas["give_feedback"]["function"]["parameters"]["required"] == []

    def test_fragment_properties_carry_names_and_types_only(self):
        blob, _ = _blob(_toggled({"intent"}))
        assert "Instruction for" not in blob
        schemas = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in json.loads(blob)}
        assert schemas["direct_scene"]["keywords"] == {"type": "array", "items": {"type": "string"}}
        assert schemas["direct_scene"]["intent"] == {"type": "string"}
        assert schemas["give_feedback"]["suggestions"] == {"type": "string"}
        assert schemas["update_state"]["facts"] == {"type": "array", "items": {"type": "string"}}

    def test_fixed_properties_keep_their_descriptions(self):
        overrides, _ = build_writer_tools_blob(_SETTINGS, _GLOBALS, {}, grouped=True)
        scene = overrides["direct_scene"]["function"]["parameters"]["properties"]
        assert scene["moods"]["description"] and scene["speaking_plan"]["description"]
        assert overrides["update_state"]["function"]["parameters"]["properties"]["retire"]["description"]
        # A fragment sharing a fixed property's id would strip its text from the blob.
        assert {"moods", SPEAKING_PLAN_FIELD, "retire"} <= RESERVED_FRAGMENT_IDS

    def test_editing_a_description_leaves_the_blob_byte_identical(self):
        reference, _ = _blob(_GLOBALS)
        edited = [{**row, "description": "rewritten"} for row in _GLOBALS]
        assert _blob(edited)[0] == reference

    def test_no_defined_fragment_of_a_kind_means_no_tool(self):
        _, enabled = _blob([row for row in _GLOBALS if row["field_type"] not in ("feedback", "post_processing")])
        assert "give_feedback" not in enabled and "editor_search_replace" not in enabled


class TestDefinedFragments:
    def test_disabled_global_yields_its_slot_to_the_card_row(self):
        card = {**_row("keywords", "array", 0), "description": "card keywords"}
        rows = _defined_fragments(_toggled({"keywords"}), [card])
        assert [row["id"] for row in rows] == [row["id"] for row in _GLOBALS]
        assert rows[2] is card

    def test_enabled_global_wins_and_new_card_rows_follow(self):
        card = [_row("keywords", "array", 0), _row("card_only", "string", 0)]
        rows = _defined_fragments(_GLOBALS, card)
        assert rows[2] is _GLOBALS[2]
        assert rows[-1] is card[1]


class TestLiveView:
    def test_keeps_live_fields_in_blob_order_and_restores_required(self):
        overrides, _ = build_writer_tools_blob(_SETTINGS, _GLOBALS, {})
        live = [_GLOBALS[2], _GLOBALS[1]]
        params = live_direct_scene_schema(overrides["direct_scene"], live)["function"]["parameters"]
        assert list(params["properties"]) == ["next_event", "keywords", "moods"]
        assert params["required"] == ["next_event"]

    def test_restores_live_descriptions_only(self):
        overrides, _ = build_writer_tools_blob(_SETTINGS, _GLOBALS, {}, grouped=True)
        props = live_direct_scene_schema(overrides["direct_scene"], [_GLOBALS[2]])["function"]["parameters"]["properties"]
        assert props["keywords"]["description"] == "Instruction for keywords"
        assert props["moods"] == overrides["direct_scene"]["function"]["parameters"]["properties"]["moods"]
        assert props["speaking_plan"]["description"] == SPEAKING_PLAN_SCHEMA_DESCRIPTION
        assert "intent" not in props

    def test_instruction_states_required_fields(self):
        overrides, _ = build_writer_tools_blob(_SETTINGS, _GLOBALS, {})
        out = tool_call_instruction("direct_scene", live_direct_scene_schema(overrides["direct_scene"], _GLOBALS[:2]))
        assert "Parameter order: (intent, next_event, moods)" in out
        assert out.endswith("Required: intent, next_event")
        assert "Required" not in tool_call_instruction("direct_scene", overrides["direct_scene"])

    def test_instruction_lists_descriptions_in_blob_order(self):
        overrides, _ = build_writer_tools_blob(_SETTINGS, _GLOBALS, {})
        live = [_GLOBALS[5], _GLOBALS[2], _GLOBALS[0]]
        out = tool_call_instruction(
            "direct_scene",
            live_direct_scene_schema(overrides["direct_scene"], live),
            fragments={row["id"]: row for row in live},
        )
        assert (
            "Parameters, in order:\n"
            "* intent (single value): Instruction for intent\n"
            "* keywords (list of strings): Instruction for keywords\n"
            "* mood_note (single value, kept across turns): Instruction for mood_note\n"
            "* moods (list of strings)\n"
            "Required: intent"
        ) in out


class _FakeClient:
    is_aborted = False


class _FakeBase:
    complete_into = CachedBase.complete_into

    def __init__(self, tools: list[dict], args: dict, name: str):
        self.tools = tools
        self.prefix: list = []
        self._message = {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": json.dumps(args)}}]}
        self.tails: list[str] = []
        self.schemas: list[dict | None] = []

    async def complete(self, *_, trailing, **kw):
        self.tails.append(trailing[-1]["content"])
        self.schemas.append(kw.get("json_schema"))
        yield {"type": "done", "message": self._message}


def _shared_tools() -> list[dict]:
    overrides, enabled = build_writer_tools_blob(_SETTINGS, _GLOBALS, {"direct_scene": True})
    return enabled_schemas(enabled, overrides)


class TestDirectorUsesTheLiveView:
    async def _run(self, live: list[dict], args: dict, resting: frozenset[str] = frozenset()):
        base = _FakeBase(_shared_tools(), args, "direct_scene")
        events = [
            e
            async for e in director_pass(
                _FakeClient(),  # type: ignore[arg-type]
                base,  # type: ignore[arg-type]
                "the user message",
                {},
                {"active_moods": []},
                [],
                live,
                {"direct_scene": True},
                resting=resting,
            )
        ]
        return base, events[-1]["result"]

    async def test_disabled_fields_are_named_narrowed_away_and_dropped(self):
        live = [row for row in _GLOBALS if row["id"] in ("next_event", "keywords")]
        base, result = await self._run(live, {"intent": "leaked", "next_event": "storm", "moods": []})
        assert "* next_event (single value): Instruction for next_event\n* keywords (list" in base.tails[0]
        assert "Instruction for intent" not in base.tails[0]
        assert "Required: next_event" in base.tails[0]
        assert "unavailable this turn (leave empty): intent, mood_note" in base.tails[0]
        assert list(base.schemas[0]["properties"]) == ["next_event", "keywords", "moods"]
        assert base.schemas[0]["properties"]["keywords"]["description"] == "Instruction for keywords"
        assert result.extra_fields == {"next_event": "storm"}
        assert result.calls[0]["arguments"] == {"next_event": "storm", "moods": []}

    async def test_per_fragment_steps_restore_their_target_description(self):
        base = _FakeBase(_shared_tools(), {}, "direct_scene")
        live = [row for row in _GLOBALS if row["id"] in ("next_event", "keywords")]
        _ = [
            e
            async for e in director_pass(
                _FakeClient(),  # type: ignore[arg-type]
                base,  # type: ignore[arg-type]
                "the user message",
                {"director_individual_fragments": 1},
                {"active_moods": []},
                [],
                live,
                {"direct_scene": True},
            )
        ]
        next_event, keywords, moods = base.schemas
        assert next_event["properties"]["next_event"]["description"] == "Instruction for next_event"  # type: ignore[index]
        assert keywords["properties"]["keywords"]["description"] == "Instruction for keywords"  # type: ignore[index]
        assert moods["properties"]["moods"]["description"].startswith("List of moods")  # type: ignore[index]
        assert not any("Instruction for intent" in tail for tail in base.tails)

    async def test_speaking_plan_step_keeps_the_blob_description(self):
        overrides, enabled = build_writer_tools_blob(_SETTINGS, _GLOBALS, {"direct_scene": True}, grouped=True)
        base = _FakeBase(enabled_schemas(enabled, overrides), {}, "direct_scene")
        live = [row for row in _GLOBALS if row["id"] == "next_event"]
        _ = [
            e
            async for e in director_pass(
                _FakeClient(),  # type: ignore[arg-type]
                base,  # type: ignore[arg-type]
                "the user message",
                {"director_individual_fragments": 1},
                {"active_moods": []},
                [],
                live,
                {"direct_scene": True},
            )
        ]
        plan = base.schemas[1]["properties"][SPEAKING_PLAN_FIELD]  # type: ignore[index]
        assert plan["description"] == SPEAKING_PLAN_SCHEMA_DESCRIPTION

    async def test_resting_fields_leave_the_live_view(self):
        live = [row for row in _GLOBALS if row["id"] in ("intent", "next_event")]
        base, result = await self._run(live, {"intent": "x", "next_event": "y"}, resting=frozenset({"intent"}))
        assert "unavailable this turn (leave empty): intent, keywords, mood_note" in base.tails[0]
        assert base.schemas[0]["required"] == ["next_event"]
        assert result.extra_fields == {"next_event": "y"}


async def test_feedback_narrows_and_drops_disabled_values():
    extra = _row("tone", "feedback", 8)
    overrides, enabled = build_writer_tools_blob(_SETTINGS, [*_GLOBALS, extra], {})
    base = _FakeBase(enabled_schemas(enabled, overrides), {"suggestions": "try this", "tone": "leaked"}, "give_feedback")
    events = [
        e
        async for e in feedback_step(
            _FakeClient(),  # type: ignore[arg-type]
            base,  # type: ignore[arg-type]
            "reply",
            {},
            [_GLOBALS[3]],
            writer_user_msg="hi",
        )
    ]
    assert list(base.schemas[0]["properties"]) == ["suggestions"]
    assert base.schemas[0]["properties"]["suggestions"]["description"] == "Instruction for suggestions"
    assert '* suggestions "Suggestions" (single value): Instruction for suggestions' in base.tails[0]
    assert "Instruction for tone" not in base.tails[0]
    assert events[-1]["result"].values == {"suggestions": "try this"}


def test_offered_state_ids_reads_the_shared_schema():
    base = _FakeBase(_shared_tools(), {}, "update_state")
    assert offered_state_ids(base) == {"facts"}  # type: ignore[arg-type]
