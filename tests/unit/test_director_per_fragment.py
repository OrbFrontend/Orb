"""Per-fragment director mode: the prompt builder and the director_pass loop.

Covers ``build_director_scene_step_prompt`` (pure) and the branch in
``director_pass`` that issues one forced ``direct_scene`` call per interactive
fragment when ``director_individual_fragments`` is on.
"""

from __future__ import annotations

import json

from backend.inference import (
    CachedBase,
    build_direct_scene_tool,
    build_director_scene_step_prompt,
)
from backend.pipeline.passes.director.director import director_pass

_MOODS = [{"id": "tense", "description": "suspenseful"}]
_FRAGMENTS = [
    {
        "id": "user_intent",
        "field_type": "string",
        "description": "what the user wants",
        "injection_label": "User intent",
        "sort_order": 1,
    },
    {"id": "keywords", "field_type": "array", "description": "key nouns", "injection_label": "Keywords", "sort_order": 2},
    {
        "id": "next_event",
        "field_type": "string",
        "description": "what happens next",
        "injection_label": "Next event",
        "sort_order": 3,
    },
]


def _ds_message(args: dict) -> dict:
    """An assistant completion carrying one forced ``direct_scene`` tool call."""
    return {"role": "assistant", "tool_calls": [{"function": {"name": "direct_scene", "arguments": json.dumps(args)}}]}


class _FakeBase:
    """Stands in for ``CachedBase``: serves canned completions and records the
    per-call request tail so feed-forward and isolation can be asserted.

    ``complete_into`` is borrowed from the real class rather than restated, so
    the pass's event demux under test is the shipped one.
    """

    complete_into = CachedBase.complete_into

    def __init__(self, fragments: list[dict], responses: list[dict]):
        self.tools = [build_direct_scene_tool(fragments)]
        self.prefix: list = []
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.schemas: list[dict | None] = []

    async def complete(self, *_, label, trailing, **kw):
        self.calls.append((label, trailing[0]["content"]))
        self.schemas.append(kw.get("json_schema"))
        yield {"type": "done", "message": self._responses.pop(0)}


class _FakeClient:
    is_aborted = False


async def _run(base, fragments, settings, director=None):
    events = [
        e
        async for e in director_pass(
            _FakeClient(),  # type: ignore[arg-type]
            base,
            "the user message",
            settings,
            director or {"active_moods": []},
            _MOODS,
            fragments,
            {"direct_scene": True},
        )
    ]
    return events[-1]["result"]


# ── build_director_scene_step_prompt ──────────────────────────────────────────


class TestStepPrompt:
    # These assert on what the builder *interpolates* -- fragment ids, descriptions,
    # decided values, the field-type hint -- never on the surrounding instruction
    # copy, which gets reworded whenever the director prompt is tuned.

    def test_moods_stage_targets_moods_only(self):
        out = build_director_scene_step_prompt("msg", ["tense"], _MOODS, target_fragment=None)
        assert "tense" in out and "suspenseful" in out  # the mood options block rendered
        assert "user_intent" not in out  # ...and no interactive fragment was targeted
        # Lorebook selection is no longer part of direct_scene (own select_lorebook tool).
        assert "selected_lorebook_entries" not in out

    def test_fragment_stage_targets_one_field(self):
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[0])
        assert "user_intent" in out and "what the user wants" in out
        assert "single value" in out  # field_type -> hint

    def test_array_fragment_hint(self):
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[1])
        assert "keywords" in out and "list of strings" in out

    def test_decided_fields_rendered_and_list_joined(self):
        decided = [("User intent", "wants conflict"), ("Keywords", ["desert", "knife"])]
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[2], decided_fields=decided)
        assert "- User intent: wants conflict" in out
        assert "- Keywords: desert, knife" in out

    def test_empty_decided_value_omitted(self):
        out = build_director_scene_step_prompt(
            "msg", [], _MOODS, target_fragment=_FRAGMENTS[2], decided_fields=[("Keywords", [])]
        )
        assert "Keywords" not in out

    def test_progressive_prior_line_only_when_progressive(self):
        prog = {"id": "stat", "field_type": "progressive", "description": "hp", "injection_label": "HP", "sort_order": 1}
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=prog, progressive_prior="hp 42/100")
        assert "hp 42/100" in out
        # Same prior on a non-progressive field renders no previous-value line.
        plain = build_director_scene_step_prompt(
            "msg", [], _MOODS, target_fragment=_FRAGMENTS[0], progressive_prior="hp 42/100"
        )
        assert "hp 42/100" not in plain


# ── director_pass per-fragment loop ───────────────────────────────────────────


class TestPerFragmentLoop:
    async def test_one_call_per_fragment_plus_moods(self):
        # Interactive fragments resolve first, moods last (fed the decided scene).
        responses = [
            _ds_message({"user_intent": "wants X", "moods": ["wrong"]}),
            _ds_message({"keywords": ["a", "b"], "user_intent": "override"}),
            _ds_message({}),
            _ds_message({"moods": ["tense"]}),
        ]
        base = _FakeBase(_FRAGMENTS, responses)
        result = await _run(base, _FRAGMENTS, {"director_individual_fragments": 1})

        assert len(base.calls) == 4  # one per fragment + one moods call
        assert result.active_moods == ["tense"]  # fragment-stage moods are ignored
        assert result.extra_fields == {"user_intent": "wants X", "keywords": ["a", "b"]}  # empty next_event skipped
        # Each recorded call keeps only its stage's field — extras the model
        # volunteered (moods on call 1, user_intent on call 2) are stripped.
        assert [tc["arguments"] for tc in result.calls] == [
            {"user_intent": "wants X"},
            {"keywords": ["a", "b"]},
            {},
            {"moods": ["tense"]},
        ]
        # Each step call narrows the decoding grammar to its target field only
        # (applied in text mode; the chat transport drops it).
        assert [list((s or {}).get("properties", {})) for s in base.schemas] == [
            ["user_intent"],
            ["keywords"],
            ["next_event"],
            ["moods"],
        ]

    def _toggle_on(self):
        return {"director_individual_fragments": 1}

    async def test_earlier_fragments_feed_forward(self):
        responses = [
            _ds_message({"user_intent": "wants X"}),
            _ds_message({"keywords": ["a"]}),
            _ds_message({"next_event": "she leaves"}),
            _ds_message({"moods": []}),
        ]
        base = _FakeBase(_FRAGMENTS, responses)
        await _run(base, _FRAGMENTS, self._toggle_on())
        # The keywords call (second) must show the user_intent decided in the first.
        keywords_call = base.calls[1][1]
        assert "wants X" in keywords_call

    async def test_moods_call_last_sees_decided_scene(self):
        # Moods run last and are shown the interactive fields decided this turn.
        responses = [
            _ds_message({"user_intent": "wants X"}),
            _ds_message({"keywords": ["a"]}),
            _ds_message({"next_event": "she leaves"}),
            _ds_message({"moods": ["tense"]}),
        ]
        base = _FakeBase(_FRAGMENTS, responses)
        await _run(base, _FRAGMENTS, self._toggle_on())
        moods_call = base.calls[-1][1]
        assert "Next event: she leaves" in moods_call
        assert "suspenseful" in moods_call  # the moods stage, not another fragment stage

    async def test_moods_cleared_when_omitted(self):
        # Moods stage is last; an empty moods call clears the prior active moods.
        responses = [_ds_message({"user_intent": "x"}), _ds_message({})]
        base = _FakeBase(_FRAGMENTS[:1], responses)
        result = await _run(base, _FRAGMENTS[:1], self._toggle_on(), director={"active_moods": ["pre"]})
        assert result.active_moods == []

    async def test_null_moods_clear_like_an_omission(self):
        # A model that declines the moods step by emitting `"moods": null` must
        # land on [], not None -- director_stage set()-unions active_moods, so a
        # None there aborts the whole turn.
        responses = [_ds_message({"user_intent": "x"}), _ds_message({"moods": None})]
        base = _FakeBase(_FRAGMENTS[:1], responses)
        result = await _run(base, _FRAGMENTS[:1], self._toggle_on(), director={"active_moods": ["pre"]})
        assert result.active_moods == []

    async def test_non_string_moods_are_dropped(self):
        # Nothing but a fragment id can be a mood, and an unhashable item would
        # blow up the same set() union.
        responses = [_ds_message({"user_intent": "x"}), _ds_message({"moods": ["tense", {"id": "tense"}, 7]})]
        base = _FakeBase(_FRAGMENTS[:1], responses)
        result = await _run(base, _FRAGMENTS[:1], self._toggle_on())
        assert result.active_moods == ["tense"]

    async def test_failed_fragment_call_is_skipped_not_fatal(self):
        # Second fragment's call raises; the pass must skip it and still finish,
        # so the turn keeps going (director failures are non-fatal).
        class _FlakyBase(_FakeBase):
            async def complete(self, *_, label, trailing, **__):
                self.calls.append((label, trailing[0]["content"]))
                r = self._responses.pop(0)
                if r is None:
                    raise RuntimeError("boom")
                yield {"type": "done", "message": r}

        responses = [
            _ds_message({"user_intent": "wants X"}),
            None,  # keywords call fails
            _ds_message({"next_event": "she leaves"}),
            _ds_message({"moods": ["tense"]}),
        ]
        base = _FlakyBase(_FRAGMENTS, responses)
        result = await _run(base, _FRAGMENTS, self._toggle_on())

        assert len(base.calls) == 4  # every fragment still attempted
        assert result.active_moods == ["tense"]
        assert result.extra_fields == {"user_intent": "wants X", "next_event": "she leaves"}  # failed keywords absent

    async def test_toggle_off_uses_single_call(self):
        responses = [_ds_message({"moods": ["tense"], "user_intent": "x", "keywords": ["k"]})]
        base = _FakeBase(_FRAGMENTS, responses)
        result = await _run(base, _FRAGMENTS, {"director_individual_fragments": 0})
        assert len(base.calls) == 1
        assert result.extra_fields == {"user_intent": "x", "keywords": ["k"]}
        assert result.active_moods == ["tense"]


class TestDirectSceneRequiredStripped:
    """Per-fragment mode drops `required` from the shared direct_scene blob so the
    advertised schema doesn't contradict the "Fill ONLY X, leave others empty" step
    prompt on endpoints that can't grammar-narrow the call."""

    _REQUIRED_FRAGS = [
        {"id": "problem", "field_type": "string", "description": "the problem", "sort_order": 1, "required": True},
        {"id": "next_event", "field_type": "string", "description": "what happens", "sort_order": 2, "required": True},
    ]

    def _blob(self, per_fragment: int) -> dict:
        from backend.pipeline.config import _build_writer_tools_blob

        return _build_writer_tools_blob(
            {"director_individual_fragments": per_fragment},
            self._REQUIRED_FRAGS,
            {},
        )

    def test_required_dropped_when_per_fragment_on(self):
        blob = self._blob(1)
        assert blob["direct_scene"]["function"]["parameters"]["required"] == []

    def test_required_kept_when_per_fragment_off(self):
        blob = self._blob(0)
        assert set(blob["direct_scene"]["function"]["parameters"]["required"]) == {"problem", "next_event"}
