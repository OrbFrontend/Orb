"""Unit tests for the format_consistency post-pipeline hook."""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType

import pytest

from backend.analysis.format_consistency import (
    AxisStyle,
    Dialogue,
    Narration,
    baseline_axes,
    classify_axes,
    narration_only,
)
from backend.workflows import PostCtx
from backend.workflows.format_consistency import (
    VOICE_REWRITE_LENGTH_RULE,
    VOICE_REWRITE_TOOL,
    VOICE_REWRITE_TOOL_NAME,
    capture,
    guard,
    hooks,
    voice,
)

QUOTED_BASELINE = 'She smiles. "Hello there," she says warmly.'
QUOTED_BASELINE_NARRATION = "She smiles. she says warmly."
DRIFTING_DRAFT = "*She steps closer, watching him carefully.* Are you sure about this?"
NORMALIZED = 'She steps closer, watching him carefully. "Are you sure about this?"'
CONSISTENT_DRAFT = 'He nods slowly. "I understand," he replies.'
CONSISTENT_NARRATION = "He nods slowly. he replies."

ASTERISK_MSG = "*She smiles and steps back, turning to the window.* I won't go."


WRITER_CLIENT = object()
AGENT_CLIENT = object()


def _ctx(draft: str, history: list[dict]) -> PostCtx:
    return PostCtx(
        conversation_id="c1",
        history=tuple(MappingProxyType(m) for m in history),
        draft=draft,
        effective_msg="and then?",
        director_output=MappingProxyType({}),
        settings=MappingProxyType({}),
        prefix=({"role": "system", "content": "writer base"},),
        enabled_tools=MappingProxyType({}),
        turn_scratch={},
        client=WRITER_CLIENT,
        kv_tracker=None,
        schema_overrides=MappingProxyType({}),
        character_id=None,
        agent_client=AGENT_CLIENT,
        agent_model_name="agent-model",
    )


@pytest.fixture(autouse=True)
def _classifier_absent(monkeypatch):
    """Keep markup-only tests independent of the local classifier and database."""
    monkeypatch.setattr(hooks, "local_feature_ready", lambda feature, settings: False)


async def _collect(ctx) -> list[dict]:
    return [ev async for ev in hooks.post_pipeline(ctx)]


async def test_yields_draft_replaced_on_drift():
    history = [
        {"role": "assistant", "content": QUOTED_BASELINE},
        {"role": "user", "content": "and then?"},
    ]
    events = await _collect(_ctx(DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_no_yield_when_baseline_unstable():
    history = [
        {"role": "assistant", "content": QUOTED_BASELINE},
        {"role": "assistant", "content": ASTERISK_MSG},
    ]
    events = await _collect(_ctx('She frowns. "What now?"', history))

    assert events == []


async def test_no_yield_when_already_consistent():
    history = [{"role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(CONSISTENT_DRAFT, history))

    assert events == []


async def test_no_yield_when_no_assistant_baseline():
    history = [{"role": "user", "content": "hello"}]
    events = await _collect(_ctx(DRIFTING_DRAFT, history))

    assert events == []


async def test_the_aggregate_convention_only_reaches_the_markup_target(monkeypatch):
    """Use the aggregate convention only as the markup target."""
    _voice_on(monkeypatch)
    convention = AxisStyle(Dialogue.QUOTED, Narration.BARE)
    baseline_calls: list[list[str]] = []

    def fake_baseline(messages):
        baseline_calls.append(messages)
        return convention

    async def fake_hold(ctx, text, window):
        return text

    class Unchanged:
        changed = False

    def fake_normalize(draft, messages, *, enabled, target):
        assert enabled is True
        assert target is convention
        return draft, Unchanged()

    monkeypatch.setattr(hooks, "baseline_axes", fake_baseline)
    monkeypatch.setattr(hooks, "_hold_voice", fake_hold)
    monkeypatch.setattr(hooks, "normalize_to_baseline", fake_normalize)

    events = await _collect(_ctx(CONSISTENT_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == []
    assert baseline_calls == [[QUOTED_BASELINE]]


# ---------- the voice half ----------

THIRD_PAST = ("third", "past")
SECOND_PRESENT = ("second", "present")

VOICE_DRIFTING_DRAFT = 'You step closer, watching him carefully. "Are you sure about this?"'
VOICE_DRIFTING_NARRATION = "You step closer, watching him carefully."


def _voice_on(monkeypatch, *, enabled: bool = True):
    """Model present and the opt-in config set."""
    monkeypatch.setattr(hooks, "local_feature_ready", lambda feature, settings: True)

    async def fake_config(workflow_id):
        return {"voice_consistency": enabled}

    monkeypatch.setattr(hooks, "get_workflow_config", fake_config)


def _classifier(monkeypatch, answers: dict[str, tuple[str, str]]) -> list[str]:
    """Answer from *answers*; record every text the classifier was shown."""
    seen: list[str] = []

    async def fake(text: str) -> tuple[str, str]:
        seen.append(text)
        return answers.get(text, ("ambiguous", "ambiguous"))

    monkeypatch.setattr(voice, "classify_pov_tense", fake)
    return seen


def _forced_call(monkeypatch, rewritten: str) -> list[dict]:
    """Capture the forced-call kwargs and answer with *rewritten*."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)

        async def events():
            yield {"type": "result", "args": {"rewritten_text": rewritten}}

        return events()

    monkeypatch.setattr(hooks, "forced_tool_call", fake)
    return calls


def test_voice_rewrite_declares_its_own_compatible_standalone_schema():
    function = VOICE_REWRITE_TOOL.schema["function"]

    assert VOICE_REWRITE_TOOL.name == VOICE_REWRITE_TOOL_NAME
    assert VOICE_REWRITE_TOOL.standalone is True
    assert function["name"] == VOICE_REWRITE_TOOL_NAME
    assert VOICE_REWRITE_LENGTH_RULE in function["description"]
    assert VOICE_REWRITE_LENGTH_RULE in hooks._SYSTEM
    assert "audit" not in function["description"].lower()
    assert "length constraint" not in function["description"].lower()


async def test_voice_drift_and_markup_drift_compose_into_one_event(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    history = [{"role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert len(calls) == 1


async def test_the_rewrite_runs_on_the_agent_lane(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    ctx = _ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}])
    await _collect(ctx)

    [call] = calls
    assert call["client"] is AGENT_CLIENT
    assert call["model_name"] == "agent-model"
    assert call["tool_name"] == VOICE_REWRITE_TOOL_NAME


async def test_the_rewrite_is_a_self_contained_lane(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    ctx = _ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}])
    await _collect(ctx)

    [call] = calls
    assert call["enabled_tools"] is None
    assert call["cache_shape"] == "format_consistency:voice_rewrite"
    assert call["prefix"] != ctx.prefix
    assert [m["role"] for m in call["prefix"]] == ["system"]

    [tail] = call["tail_messages"]
    assert tail["role"] == "user"
    assert VOICE_DRIFTING_DRAFT in tail["content"]
    assert QUOTED_BASELINE not in tail["content"]
    assert ctx.effective_msg not in tail["content"]


async def test_only_the_drifting_axis_is_named(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(
        monkeypatch,
        {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: ("second", "past")},
    )
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    await _collect(_ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    instruction = calls[0]["tail_messages"][0]["content"]
    assert "third person" in instruction
    assert "tense" not in instruction


async def test_consistent_voice_makes_no_llm_call(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, CONSISTENT_NARRATION: THIRD_PAST})
    calls = _forced_call(monkeypatch, "should not be used")

    events = await _collect(_ctx(CONSISTENT_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == []
    assert calls == []


async def test_an_unstable_baseline_voice_makes_no_llm_call(monkeypatch):
    _voice_on(monkeypatch)
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, ASTERISK_MSG: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, "should not be used")

    history = [
        {"role": "assistant", "content": QUOTED_BASELINE},
        {"role": "assistant", "content": ASTERISK_MSG},
    ]
    await _collect(_ctx(CONSISTENT_DRAFT, history))

    assert calls == []
    assert CONSISTENT_DRAFT not in seen


async def test_config_off_classifies_nothing(monkeypatch):
    _voice_on(monkeypatch, enabled=False)
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, "should not be used")

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert seen == []
    assert calls == []


async def test_classifier_absent_never_reads_the_config_slot(monkeypatch):
    async def boom(workflow_id):
        raise AssertionError("the config slot must not be read without the classifier")

    monkeypatch.setattr(hooks, "get_workflow_config", boom)

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_a_raising_classifier_degrades_instead_of_aborting(monkeypatch):
    _voice_on(monkeypatch)

    async def boom(text: str):
        raise RuntimeError("model failed to load")

    monkeypatch.setattr(voice, "classify_pov_tense", boom)
    calls = _forced_call(monkeypatch, "should not be used")

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert calls == []


async def test_a_raising_forced_call_degrades_instead_of_aborting(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})

    def boom(**kwargs):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(hooks, "forced_tool_call", boom)

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_an_unreachable_config_slot_still_normalizes_markup(monkeypatch):
    monkeypatch.setattr(hooks, "local_feature_ready", lambda feature, settings: True)

    async def boom(workflow_id):
        raise RuntimeError("no such table: workflow_config")

    monkeypatch.setattr(hooks, "get_workflow_config", boom)

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_an_unreachable_label_cache_still_normalizes_markup(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})

    async def boom(message_id, workflow_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(voice, "get_workflow_message_state", boom)

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_an_empty_rewrite_falls_through_to_the_markup_path(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
    _forced_call(monkeypatch, "")

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_a_cached_message_id_is_not_reclassified(monkeypatch):
    _voice_on(monkeypatch)
    seen = _classifier(monkeypatch, {VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    _forced_call(monkeypatch, DRIFTING_DRAFT)

    async def cached(message_id, workflow_id):
        assert (message_id, workflow_id) == (7, "format_consistency")
        return {
            "pov": "third",
            "tense": "past",
            "dialogue": "quoted",
            "content_sha256": voice._content_digest(QUOTED_BASELINE),
            "classifier": voice.local_model_identity(voice.FEATURE),
        }

    async def no_write(message_id, workflow_id, payload):
        raise AssertionError("a cache hit must not write")

    monkeypatch.setattr(voice, "get_workflow_message_state", cached)
    monkeypatch.setattr(voice, "set_workflow_message_state", no_write)

    history = [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert seen == [VOICE_DRIFTING_NARRATION]


async def test_a_cache_miss_backfills_the_labels(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, CONSISTENT_NARRATION: THIRD_PAST})
    written: list[tuple] = []

    async def empty(message_id, workflow_id):
        return None

    async def record(message_id, workflow_id, payload):
        written.append((message_id, workflow_id, payload))

    monkeypatch.setattr(voice, "get_workflow_message_state", empty)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)

    history = [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]
    await _collect(_ctx(CONSISTENT_DRAFT, history))

    assert written == [
        (
            7,
            "format_consistency",
            {
                "pov": "third",
                "tense": "past",
                "dialogue": "quoted",
                "content_sha256": voice._content_digest(QUOTED_BASELINE),
                "classifier": voice.local_model_identity(voice.FEATURE),
            },
        )
    ]


async def test_bare_dialogue_is_removed_before_voice_classification(monkeypatch):
    """Classify only the narration from bare-dialogue messages."""
    _voice_on(monkeypatch)
    baseline = (
        "As president of the Literature Club, it's my duty to make the club fun and "
        "exciting for everyone! *Monika smiles kindly at you.* Tell me, what brings you here today?"
    )
    draft = "Welcome to the club. *Monika waits by the desk.* Please, take a seat."
    seen = _classifier(
        monkeypatch,
        {
            "Monika smiles kindly at you.": ("third", "present"),
            "Monika waits by the desk.": ("third", "present"),
        },
    )
    calls = _forced_call(monkeypatch, "should not be used")

    events = await _collect(_ctx(draft, [{"role": "assistant", "content": baseline}]))

    assert events == []
    assert calls == []
    assert seen == ["Monika smiles kindly at you.", "Monika waits by the desk."]


@pytest.mark.parametrize("cached_dialogue", [None, "quoted"])
async def test_labels_are_reclassified_when_the_cached_convention_differs(monkeypatch, cached_dialogue):
    """Reclassify cached labels when the message's convention changes."""
    msg = {
        "id": 7,
        "role": "assistant",
        "content": "Stay with me. *Monika waits by the desk.* We can talk here.",
    }
    cached_payload = {"pov": "second", "tense": "present", "other": "preserved"}
    cached_payload["content_sha256"] = voice._content_digest(msg["content"])
    cached_payload["classifier"] = voice.local_model_identity(voice.FEATURE)
    if cached_dialogue is not None:
        cached_payload["dialogue"] = cached_dialogue
    seen = _classifier(monkeypatch, {"Monika waits by the desk.": THIRD_PAST})
    written: list[dict] = []

    async def cached(message_id, workflow_id):
        return cached_payload

    async def record(message_id, workflow_id, payload):
        written.append(payload)

    monkeypatch.setattr(voice, "get_workflow_message_state", cached)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)

    assert classify_axes(msg["content"]).dialogue == Dialogue.BARE
    assert await voice.labels_for(msg) == THIRD_PAST
    assert seen == ["Monika waits by the desk."]
    assert written == [
        {
            "pov": "third",
            "tense": "past",
            "other": "preserved",
            "dialogue": "bare",
            "content_sha256": voice._content_digest(msg["content"]),
            "classifier": voice.local_model_identity(voice.FEATURE),
        }
    ]


async def test_labels_are_reclassified_when_message_content_changes(monkeypatch):
    msg = {"id": 7, "role": "assistant", "content": QUOTED_BASELINE}
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST})
    written: list[dict] = []

    async def stale(message_id, workflow_id):
        return {
            "pov": "second",
            "tense": "present",
            "dialogue": "quoted",
            "content_sha256": voice._content_digest("You wait by the door."),
            "classifier": voice.local_model_identity(voice.FEATURE),
        }

    async def record(message_id, workflow_id, payload):
        written.append(payload)

    monkeypatch.setattr(voice, "get_workflow_message_state", stale)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)

    assert await voice.labels_for(msg) == THIRD_PAST
    assert seen == [QUOTED_BASELINE_NARRATION]
    assert written[0]["content_sha256"] == voice._content_digest(QUOTED_BASELINE)


@pytest.mark.parametrize("classifier", [None, "chartreuse-verte/ettin-povtense-17m@old-revision"])
async def test_old_model_labels_are_reclassified(monkeypatch, classifier):
    msg = {"id": 7, "role": "assistant", "content": QUOTED_BASELINE}
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST})
    written = []

    async def stale(message_id, workflow_id):
        return {
            "pov": "second",
            "tense": "present",
            "dialogue": "quoted",
            "content_sha256": voice._content_digest(QUOTED_BASELINE),
            "classifier": classifier,
            "other": "preserved",
        }

    async def record(message_id, workflow_id, payload):
        written.append(payload)

    monkeypatch.setattr(voice, "get_workflow_message_state", stale)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)
    assert await voice.labels_for(msg) == THIRD_PAST
    assert seen == [QUOTED_BASELINE_NARRATION]
    assert written[0]["classifier"] == voice.local_model_identity(voice.FEATURE)
    assert written[0]["other"] == "preserved"


async def test_cached_labels_are_refreshed_after_the_extraction_policy_changes(monkeypatch):
    text = "*You can do this. You have to keep moving.* she thinks, waiting."
    seen = _classifier(monkeypatch, {"she thinks, waiting.": ("third", "present")})
    written = []

    async def stale(message_id, workflow_id):
        return {
            "pov": "ambiguous",
            "tense": "ambiguous",
            "dialogue": classify_axes(text).dialogue.value,
            "classifier": voice.local_model_identity(voice.FEATURE),
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }

    async def record(message_id, workflow_id, payload):
        written.append(payload)

    monkeypatch.setattr(voice, "get_workflow_message_state", stale)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)
    assert await voice.labels_for({"id": 7, "content": text}) == ("third", "present")
    assert seen == ["she thinks, waiting."]
    assert written[0]["content_sha256"] == voice._content_digest(text)


async def test_classifier_failure_is_not_cached(monkeypatch):
    async def empty(message_id, workflow_id):
        return None

    async def boom(text: str):
        raise RuntimeError("model failed to load")

    async def no_write(message_id, workflow_id, payload):
        raise AssertionError("a transient failure must not poison the cache")

    monkeypatch.setattr(voice, "get_workflow_message_state", empty)
    monkeypatch.setattr(voice, "set_workflow_message_state", no_write)
    monkeypatch.setattr(voice, "classify_pov_tense", boom)

    msg = {"id": 7, "role": "assistant", "content": QUOTED_BASELINE}
    assert await voice.labels_for(msg) is None


# ---------- source parsing: every text under its own convention ----------


async def test_a_quoted_draft_is_parsed_as_quoted_in_a_bare_dialogue_chat(monkeypatch):
    """Parse a quoted draft under its own convention."""
    _voice_on(monkeypatch)
    baseline = "*She smiles, stepping back toward the window.* Hello there."
    draft = 'You step closer, watching him. "Are you sure about this?"'
    draft_narration = "You step closer, watching him."

    assert baseline_axes([baseline]).dialogue == Dialogue.BARE
    assert narration_only(draft, Dialogue.BARE) == ""  # what the old code passed on

    seen = _classifier(
        monkeypatch,
        {"She smiles, stepping back toward the window.": THIRD_PAST, draft_narration: SECOND_PRESENT},
    )
    calls = _forced_call(monkeypatch, "*She steps closer, watching him.* Are you sure about this?")

    await _collect(_ctx(draft, [{"role": "assistant", "content": baseline}]))

    assert seen == ["She smiles, stepping back toward the window.", draft_narration]
    assert len(calls) == 1  # the drift was seen, not swallowed


async def test_a_bare_dialogue_draft_keeps_its_speech_out_of_the_classifier(monkeypatch):
    """Keep bare-dialogue speech out of voice classification."""
    _voice_on(monkeypatch)
    draft = "*She waits by the desk.* Tell me, what brings you here today?"

    assert classify_axes(draft).dialogue == Dialogue.BARE
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, "She waits by the desk.": THIRD_PAST})
    calls = _forced_call(monkeypatch, "should not be used")

    await _collect(_ctx(draft, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert seen == [QUOTED_BASELINE_NARRATION, "She waits by the desk."]
    assert calls == []  # both ends third/past: no drift, and no speech voted


async def test_each_history_row_is_classified_under_its_own_convention(monkeypatch):
    """Classify each history row under its own convention."""
    _voice_on(monkeypatch)
    bare_row = "*She smiles, stepping back toward the window.* Hello there."
    quoted_row = 'He nods slowly. "I understand," he replies.'

    assert classify_axes(bare_row).dialogue == Dialogue.BARE
    assert classify_axes(quoted_row).dialogue == Dialogue.QUOTED

    seen = _classifier(
        monkeypatch,
        {
            "She smiles, stepping back toward the window.": THIRD_PAST,
            CONSISTENT_NARRATION: THIRD_PAST,
            VOICE_DRIFTING_NARRATION: SECOND_PRESENT,
        },
    )
    _forced_call(monkeypatch, CONSISTENT_DRAFT)

    history = [
        {"role": "assistant", "content": quoted_row},
        {"role": "assistant", "content": bare_row},
    ]
    await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert seen == [
        "She smiles, stepping back toward the window.",
        CONSISTENT_NARRATION,
        VOICE_DRIFTING_NARRATION,
    ]


async def test_a_changed_window_majority_does_not_invalidate_a_cached_row(monkeypatch):
    """Changing nearby messages does not invalidate an unchanged cached row."""
    _voice_on(monkeypatch)
    row = {"id": 7, "role": "assistant", "content": QUOTED_BASELINE}
    cache = {
        "pov": "third",
        "tense": "past",
        "dialogue": "quoted",
        "content_sha256": voice._content_digest(QUOTED_BASELINE),
        "classifier": voice.local_model_identity(voice.FEATURE),
    }

    async def cached(message_id, workflow_id):
        return cache

    async def no_write(message_id, workflow_id, payload):
        raise AssertionError("an unchanged message must not be reclassified")

    monkeypatch.setattr(voice, "get_workflow_message_state", cached)
    monkeypatch.setattr(voice, "set_workflow_message_state", no_write)
    seen = _classifier(monkeypatch, {})

    for neighbour in (CONSISTENT_DRAFT, ASTERISK_MSG):
        assert await voice.labels_for(row) == THIRD_PAST
        assert baseline_axes([QUOTED_BASELINE, neighbour]) is not None

    assert seen == []


# ---------- the rewrite is not trusted on sight ----------


async def test_a_rewrite_that_changes_the_story_is_discarded(monkeypatch):
    """Discard a rewrite that changes dialogue content."""
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    _forced_call(monkeypatch, "*She steps closer, watching him carefully.* Are you certain about this?")

    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == []
    assert guard.rejection(VOICE_DRIFTING_DRAFT, "*She steps closer.* Are you certain about this?")


async def test_a_rewrite_that_regrows_past_the_editor_is_discarded(monkeypatch):
    """The Editor may have just cut this draft for the length guard."""
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    _forced_call(
        monkeypatch,
        "She steps closer, watching him with great care and no small amount of worry, "
        'and after a long moment she finally speaks. "Are you sure about this?"',
    )

    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == []


async def test_a_rewrite_that_only_moved_the_markup_is_still_accepted(monkeypatch):
    """Accept a faithful rewrite even when its markup convention changes."""
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    _forced_call(monkeypatch, DRIFTING_DRAFT)

    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


# ---------- opt-in capture of the normalizer's exact inputs ----------


async def test_markup_capture_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv(capture.ENV, raising=False)
    written: list[str] = []
    monkeypatch.setattr(capture, "_append", lambda path, line: written.append(line))

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert written == []


async def test_markup_capture_records_the_draft_the_normalizer_received(monkeypatch, tmp_path):
    path = tmp_path / "capture.jsonl"
    monkeypatch.setenv(capture.ENV, str(path))
    history = [
        {"id": 3, "role": "assistant", "content": QUOTED_BASELINE},
        {"id": 4, "role": "user", "content": "and then?"},
    ]

    await _collect(_ctx(DRIFTING_DRAFT, history))

    [row] = [json.loads(line) for line in path.read_text().splitlines()]
    assert row["draft"] == row["hook_input"] == DRIFTING_DRAFT
    assert row["output"] == NORMALIZED
    assert row["baseline"] == [{"id": 3, "content": QUOTED_BASELINE}]
    assert row["parent_id"] == 4
    assert row["source"] == {"narration": "asterisk", "dialogue": "bare"}
    assert row["target"] == {"narration": "bare", "dialogue": "quoted"}
    assert (row["note"], row["changed"]) == ("normalized", True)


async def test_a_failing_capture_never_blocks_normalization(monkeypatch, tmp_path):
    monkeypatch.setenv(capture.ENV, str(tmp_path))  # a directory: the append fails

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
