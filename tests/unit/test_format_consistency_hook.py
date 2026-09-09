"""Unit tests for the format_consistency workflow's post_pipeline hook.

These stay true unit tests (no DB, no model, no Codex-sandbox aiosqlite caveat).
The pure normalizer the hook calls is covered exhaustively by
``tests/unit/test_format_consistency.py``; here we pin the hook's wiring: the
baseline reconstructed from ``ctx.history`` and the ``draft_replaced`` event
shape.

The markup half has no on/off decision of its own -- the framework's per-workflow
toggle suspends it in the fan-out loop. The voice half does: it is opt-in config
AND needs a local classifier, so the autouse fixture below reports that
classifier absent, which is what keeps the markup-only tests free of both a DB
read and a model. Tests that want the voice path turn it on explicitly.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from backend.analysis import AxisStyle, Dialogue, Narration
from backend.workflows import PostCtx
from backend.workflows.format_consistency import (
    VOICE_REWRITE_LENGTH_RULE,
    VOICE_REWRITE_TOOL,
    VOICE_REWRITE_TOOL_NAME,
    hooks,
    voice,
)

# A single QUOTED-convention baseline; an asterisk-narration draft drifts from it
# and is rewritten (lifted from the pure-logic suite's inversion case).
QUOTED_BASELINE = 'She smiles. "Hello there," she says warmly.'
QUOTED_BASELINE_NARRATION = "She smiles. she says warmly."
DRIFTING_DRAFT = "*She steps closer, watching him carefully.* Are you sure about this?"
NORMALIZED = 'She steps closer, watching him carefully. "Are you sure about this?"'
CONSISTENT_DRAFT = 'He nods slowly. "I understand," he replies.'
CONSISTENT_NARRATION = "He nods slowly. he replies."

# Conflicting conventions in the window -> no axis agrees -> nothing to enforce.
ASTERISK_MSG = "*She smiles and steps back, turning to the window.* I won't go."


WRITER_CLIENT = object()
AGENT_CLIENT = object()
AGENT_PREFIX = ({"role": "system", "content": "agent base"},)


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
        agent_prefix=AGENT_PREFIX,
    )


@pytest.fixture(autouse=True)
def _classifier_absent(monkeypatch):
    """No POV/tense model by default, so the voice half never runs.

    Without this the suite's answers would depend on whether the machine running
    it happens to have the GGUF on disk -- and a machine that does would send
    these unit tests to the config slot in the database.
    """
    monkeypatch.setattr(voice, "local_feature_available", lambda feature: (False, "not installed"))


async def _collect(ctx) -> list[dict]:
    return [ev async for ev in hooks.post_pipeline(ctx)]


async def test_yields_draft_replaced_on_drift():
    # No config patch: the hook runs unconditionally now (the framework toggle is
    # the only on/off). A user message is interleaved to confirm the baseline
    # window skips it.
    history = [
        {"role": "assistant", "content": QUOTED_BASELINE},
        {"role": "user", "content": "and then?"},
    ]
    events = await _collect(_ctx(DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_no_yield_when_baseline_unstable():
    # Two assistant messages with conflicting conventions -> neither axis agrees.
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
    # Only user messages: the window is empty, so the normalizer no-ops.
    history = [{"role": "user", "content": "hello"}]
    events = await _collect(_ctx(DRIFTING_DRAFT, history))

    assert events == []


async def test_one_baseline_convention_drives_voice_and_markup(monkeypatch):
    """The two repairs must not independently reinterpret the same window."""
    _voice_on(monkeypatch)
    convention = AxisStyle(Dialogue.QUOTED, Narration.BARE)
    baseline_calls: list[list[str]] = []

    def fake_baseline(messages):
        baseline_calls.append(messages)
        return convention

    async def fake_hold(ctx, text, window, received):
        assert received is convention
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
# Baseline voice and draft voice are supplied by a fake classifier keyed on the
# text it is shown: the real model is a 20MB download and its labels are not the
# thing under test here -- the wiring around them is.

THIRD_PAST = ("third", "past")
SECOND_PRESENT = ("second", "present")

# Drifts in voice AND in markup, so one turn can prove the two repairs compose.
VOICE_DRIFTING_DRAFT = 'You step closer, watching him carefully. "Are you sure about this?"'
VOICE_DRIFTING_NARRATION = "You step closer, watching him carefully."


def _voice_on(monkeypatch, *, enabled: bool = True):
    """Model present and the opt-in config set."""
    monkeypatch.setattr(voice, "local_feature_available", lambda feature: (True, ""))

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
    # The hook has a one-draft_replaced budget (the bridge warns and drops a
    # second), so the LLM rewrite and the algorithmic markup fix must arrive
    # together -- and the markup pass must run on the REWRITE, not on the draft:
    # the rewrite below comes back in asterisk markup the baseline does not use.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    history = [{"role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert len(calls) == 1


async def test_the_rewrite_runs_on_the_agent_lane(monkeypatch):
    # Still the Agent model: this is a forced tool call, and in dual-model mode the
    # writer lane is the one the pipeline strips schemas from.
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
    # Restating a passage is closed over that passage, so the call carries no
    # conversation: a constant system prefix and one user message holding the
    # target voice and the draft. Two things follow, and both are the point.
    #
    # It cannot bill the scene. ctx.agent_prefix is the turn's whole prompt, and on
    # a metered endpoint sending it on every drifting turn is the entire cost of
    # this feature.
    #
    # It cannot diverge from the turn's tool blob, because it does not use it.
    # enabled_tools=None is forced_tool_call's "ship the forced tool alone"; passing
    # ctx.enabled_tools would make it append voice_rewrite to an array the
    # Director and Writer had already sent without it, and a tools region that
    # renders ahead of history evicts the conversation behind it.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    ctx = _ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}])
    await _collect(ctx)

    [call] = calls
    assert call["enabled_tools"] is None
    assert call["cache_shape"] == "format_consistency:voice_rewrite"
    assert call["prefix"] != AGENT_PREFIX
    assert [m["role"] for m in call["prefix"]] == ["system"]

    [tail] = call["tail_messages"]
    assert tail["role"] == "user"
    # The draft rides the tail, so the system prefix stays byte-identical across
    # every rewrite and warms one lane for all of them.
    assert VOICE_DRIFTING_DRAFT in tail["content"]
    assert QUOTED_BASELINE not in tail["content"]
    assert ctx.effective_msg not in tail["content"]


async def test_only_the_drifting_axis_is_named(monkeypatch):
    # Independent axes: a window may have settled its tense while its POV moves.
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
    # Two assistant messages disagreeing on both axes -> neither is enforced, and
    # the draft is never classified at all.
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

    # The markup half is unaffected by the voice opt-in.
    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert seen == []
    assert calls == []


async def test_classifier_absent_never_reads_the_config_slot(monkeypatch):
    # The free precondition is asked first, so a host without the model pays no
    # config round trip -- and the markup path stays byte-identical to before the
    # voice half existed. (The autouse fixture is what reports it absent.)
    async def boom(workflow_id):
        raise AssertionError("the config slot must not be read without the classifier")

    monkeypatch.setattr(hooks, "get_workflow_config", boom)

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_a_raising_classifier_degrades_instead_of_aborting(monkeypatch):
    # A local-ML fault costs the voice check, not the turn.
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
    # Markup normalization is this workflow's always-on job. The voice half reaches
    # four things that can fail independently (model, config slot, message-state
    # cache, LLM endpoint); none of them may cost the markup fix.
    monkeypatch.setattr(voice, "local_feature_available", lambda feature: (True, ""))

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
    # A forced call that fills nothing must not blank the reply.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
    _forced_call(monkeypatch, "")

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_a_cached_message_id_is_not_reclassified(monkeypatch):
    # Steady state is cache hits for the window and one classification of the
    # draft. History rows in production carry an id; the cache is keyed on it.
    _voice_on(monkeypatch)
    seen = _classifier(monkeypatch, {VOICE_DRIFTING_NARRATION: SECOND_PRESENT})
    _forced_call(monkeypatch, DRIFTING_DRAFT)

    async def cached(message_id, workflow_id):
        assert (message_id, workflow_id) == (7, "format_consistency")
        return {"pov": "third", "tense": "past", "dialogue": "quoted"}

    async def no_write(message_id, workflow_id, payload):
        raise AssertionError("a cache hit must not write")

    monkeypatch.setattr(voice, "get_workflow_message_state", cached)
    monkeypatch.setattr(voice, "set_workflow_message_state", no_write)

    history = [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert seen == [VOICE_DRIFTING_NARRATION]  # the draft only; the window was cached


async def test_a_cache_miss_backfills_the_labels(monkeypatch):
    # History predating the toggle carries no labels, so the read backfills them.
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

    assert written == [(7, "format_consistency", {"pov": "third", "tense": "past", "dialogue": "quoted"})]


async def test_bare_dialogue_is_removed_before_voice_classification(monkeypatch):
    """The markup classifier's answer must shape both voice-classifier inputs.

    The baseline is mostly first/second-person unmarked speech. Feeding the raw
    messages to the voice model would make that speech define the baseline POV;
    only the asterisk action beats belong to the narrator.
    """
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
    """Legacy cache rows and rows shaped under another dialogue axis are stale."""
    convention = AxisStyle(Dialogue.BARE, Narration.ASTERISK)
    msg = {
        "id": 7,
        "role": "assistant",
        "content": "Stay with me. *Monika waits by the desk.* We can talk here.",
    }
    cached_payload = {"pov": "second", "tense": "present", "other": "preserved"}
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

    assert await voice.labels_for(msg, convention) == THIRD_PAST
    assert seen == ["Monika waits by the desk."]
    assert written == [{"pov": "third", "tense": "past", "other": "preserved", "dialogue": "bare"}]
