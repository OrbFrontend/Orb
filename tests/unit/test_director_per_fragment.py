"""Per-fragment director mode: the prompt builder and the director_pass loop.

Covers ``build_director_scene_step_prompt`` (pure) and the branch in
``director_pass`` that issues one forced ``direct_scene`` call per interactive
fragment when ``director_individual_fragments`` is on.
"""

from __future__ import annotations

import json

from backend.inference import build_direct_scene_tool, build_director_scene_step_prompt
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
    per-call request tail so feed-forward and isolation can be asserted."""

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
    def test_moods_stage_targets_moods_only(self):
        out = build_director_scene_step_prompt("msg", ["tense"], _MOODS, target_fragment=None)
        assert "Fill ONLY: moods" in out
        # Lorebook selection is no longer part of direct_scene (own select_lorebook tool).
        assert "selected_lorebook_entries" not in out
        assert "Available writing moods" in out
        assert "[tense]" in out

    def test_fragment_stage_targets_one_field(self):
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[0])
        assert "Fill ONLY the 'user_intent' parameter" in out
        assert "Field 'user_intent' (single value): what the user wants" in out

    def test_array_fragment_hint(self):
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[1])
        assert "Field 'keywords' (list of strings):" in out

    def test_decided_fields_rendered_and_list_joined(self):
        decided = [("User intent", "wants conflict"), ("Keywords", ["desert", "knife"])]
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[2], decided_fields=decided)
        assert "Decided so far this turn" in out
        assert "- User intent: wants conflict" in out
        assert "- Keywords: desert, knife" in out

    def test_empty_decided_value_omitted(self):
        out = build_director_scene_step_prompt(
            "msg", [], _MOODS, target_fragment=_FRAGMENTS[2], decided_fields=[("Keywords", [])]
        )
        assert "Decided so far this turn" not in out

    def test_progressive_prior_line_only_when_progressive(self):
        prog = {"id": "stat", "field_type": "progressive", "description": "hp", "injection_label": "HP", "sort_order": 1}
        out = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=prog, progressive_prior="10")
        assert "Previous value (update it): 10" in out
        # Same prior on a non-progressive field renders no previous-value line.
        plain = build_director_scene_step_prompt("msg", [], _MOODS, target_fragment=_FRAGMENTS[0], progressive_prior="10")
        assert "Previous value" not in plain


# ── director_pass per-fragment loop ───────────────────────────────────────────


class TestPerFragmentLoop:
    async def test_one_call_per_fragment_plus_moods(self):
        responses = [
            _ds_message({"moods": ["tense"]}),
            _ds_message({"user_intent": "wants X", "moods": ["wrong"]}),
            _ds_message({"keywords": ["a", "b"], "user_intent": "override"}),
            _ds_message({}),
        ]
        base = _FakeBase(_FRAGMENTS, responses)
        result = await _run(base, _FRAGMENTS, {"director_individual_fragments": 1})

        assert len(base.calls) == 4  # one moods call + one per fragment
        assert result.active_moods == ["tense"]  # fragment-stage moods are ignored
        assert result.extra_fields == {"user_intent": "wants X", "keywords": ["a", "b"]}  # empty next_event skipped
        # Each recorded call keeps only its stage's field — extras the model
        # volunteered (moods on call 2, user_intent on call 3) are stripped.
        assert [tc["arguments"] for tc in result.calls] == [
            {"moods": ["tense"]},
            {"user_intent": "wants X"},
            {"keywords": ["a", "b"]},
            {},
        ]
        # Each step call narrows the decoding grammar to its target field only
        # (applied in text mode; the chat transport drops it).
        assert [list((s or {}).get("properties", {})) for s in base.schemas] == [
            ["moods"],
            ["user_intent"],
            ["keywords"],
            ["next_event"],
        ]

    def _toggle_on(self):
        return {"director_individual_fragments": 1}

    async def test_earlier_fragments_feed_forward(self):
        responses = [
            _ds_message({"moods": []}),
            _ds_message({"user_intent": "wants X"}),
            _ds_message({"keywords": ["a"]}),
            _ds_message({"next_event": "she leaves"}),
        ]
        base = _FakeBase(_FRAGMENTS, responses)
        await _run(base, _FRAGMENTS, self._toggle_on())
        # The third call (keywords) must show the user_intent decided in call two.
        keywords_call = base.calls[2][1]
        assert "Decided so far this turn" in keywords_call
        assert "wants X" in keywords_call

    async def test_moods_cleared_when_omitted(self):
        responses = [_ds_message({}), _ds_message({"user_intent": "x"})]
        base = _FakeBase(_FRAGMENTS[:1], responses)
        result = await _run(base, _FRAGMENTS[:1], self._toggle_on(), director={"active_moods": ["pre"]})
        assert result.active_moods == []

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
            _ds_message({"moods": ["tense"]}),
            _ds_message({"user_intent": "wants X"}),
            None,  # keywords call fails
            _ds_message({"next_event": "she leaves"}),
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
