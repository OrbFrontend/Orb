"""Enabling or disabling a fragment never rewrites the shared tools blob.

The blob renders ahead of the conversation in the cached prefix, so the
fragment-built tools offer every defined fragment and each call narrows to its
live fields per call (see "Treat tools as part of the prompt" in
docs/architecture/kv-cache.md).
"""

from __future__ import annotations

import json

from backend.inference import CachedBase
from backend.pipeline.config import _build_writer_tools_blob
from backend.pipeline.context import _defined_fragments
from backend.pipeline.passes._prompting import tool_call_instruction
from backend.pipeline.passes.director.director import (
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
    overrides, enabled = _build_writer_tools_blob(
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
        overrides, _ = _build_writer_tools_blob(_SETTINGS, _GLOBALS, {})
        live = [_GLOBALS[2], _GLOBALS[1]]
        params = live_direct_scene_schema(overrides["direct_scene"], live)["function"]["parameters"]
        assert list(params["properties"]) == ["next_event", "keywords", "moods"]
        assert params["required"] == ["next_event"]

    def test_instruction_states_required_fields(self):
        overrides, _ = _build_writer_tools_blob(_SETTINGS, _GLOBALS, {})
        out = tool_call_instruction("direct_scene", live_direct_scene_schema(overrides["direct_scene"], _GLOBALS[:2]))
        assert "Parameter order: (intent, next_event, moods)" in out
        assert out.endswith("Required: intent, next_event")
        assert "Required" not in tool_call_instruction("direct_scene", overrides["direct_scene"])


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
    overrides, enabled = _build_writer_tools_blob(_SETTINGS, _GLOBALS, {"direct_scene": True})
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
        assert "Parameter order: (next_event, keywords, moods)" in base.tails[0]
        assert "Required: next_event" in base.tails[0]
        assert "(unavailable this turn): intent, mood_note" in base.tails[0]
        assert list(base.schemas[0]["properties"]) == ["next_event", "keywords", "moods"]
        assert result.extra_fields == {"next_event": "storm"}
        assert result.calls[0]["arguments"] == {"next_event": "storm", "moods": []}

    async def test_resting_fields_leave_the_live_view(self):
        live = [row for row in _GLOBALS if row["id"] in ("intent", "next_event")]
        base, result = await self._run(live, {"intent": "x", "next_event": "y"}, resting=frozenset({"intent"}))
        assert "(unavailable this turn): intent, keywords, mood_note" in base.tails[0]
        assert base.schemas[0]["required"] == ["next_event"]
        assert result.extra_fields == {"next_event": "y"}


async def test_feedback_narrows_and_drops_disabled_values():
    extra = _row("tone", "feedback", 8)
    overrides, enabled = _build_writer_tools_blob(_SETTINGS, [*_GLOBALS, extra], {})
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
    assert events[-1]["result"].values == {"suggestions": "try this"}


def test_offered_state_ids_reads_the_shared_schema():
    base = _FakeBase(_shared_tools(), {}, "update_state")
    assert offered_state_ids(base) == {"facts"}  # type: ignore[arg-type]
