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

from backend.workflows import PostCtx
from backend.workflows.format_consistency import hooks, voice

# A single QUOTED-convention baseline; an asterisk-narration draft drifts from it
# and is rewritten (lifted from the pure-logic suite's inversion case).
QUOTED_BASELINE = 'She smiles. "Hello there," she says warmly.'
DRIFTING_DRAFT = "*She steps closer, watching him carefully.* Are you sure about this?"
NORMALIZED = 'She steps closer, watching him carefully. "Are you sure about this?"'
CONSISTENT_DRAFT = 'He nods slowly. "I understand," he replies.'

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


# ---------- the voice half ----------
# Baseline voice and draft voice are supplied by a fake classifier keyed on the
# text it is shown: the real model is a 20MB download and its labels are not the
# thing under test here -- the wiring around them is.

THIRD_PAST = ("third", "past")
SECOND_PRESENT = ("second", "present")

# Drifts in voice AND in markup, so one turn can prove the two repairs compose.
VOICE_DRIFTING_DRAFT = 'You step closer, watching him carefully. "Are you sure about this?"'


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


async def test_voice_drift_and_markup_drift_compose_into_one_event(monkeypatch):
    # The hook has a one-draft_replaced budget (the bridge warns and drops a
    # second), so the LLM rewrite and the algorithmic markup fix must arrive
    # together -- and the markup pass must run on the REWRITE, not on the draft:
    # the rewrite below comes back in asterisk markup the baseline does not use.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, VOICE_DRIFTING_DRAFT: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    history = [{"role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert len(calls) == 1


async def test_the_rewrite_runs_on_the_agent_lane(monkeypatch):
    # In dual-model mode the pipeline strips tool schemas from the writer lane,
    # so forcing a tool call on ctx.client would send a schema to the one lane
    # that has none, against a prefix that is not the agent's.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, VOICE_DRIFTING_DRAFT: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    ctx = _ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}])
    await _collect(ctx)

    [call] = calls
    assert call["client"] is AGENT_CLIENT
    assert call["prefix"] is AGENT_PREFIX
    assert call["model_name"] == "agent-model"
    assert call["tool_name"] == "editor_rewrite"
    # The tail mirrors the editor's own trailing, so this extends the prefix the
    # editor just warmed rather than opening a new one. The per-call instruction
    # rides the tail; nothing enters the prefix.
    assert [m["role"] for m in call["tail_messages"]] == ["user", "assistant", "user"]
    assert call["tail_messages"][0]["content"] == ctx.effective_msg
    assert call["tail_messages"][1]["content"] == VOICE_DRIFTING_DRAFT
    # Only the drifting axes are named, and both of them are.
    assert "third person and past tense" in call["tail_messages"][2]["content"]
    # The pipeline's tool blob goes through untouched -- overlaying it would break
    # the byte-identical array the cached prefix is keyed on.
    assert call["enabled_tools"] is ctx.enabled_tools
    assert call["schema_overrides"] is ctx.schema_overrides


async def test_only_the_drifting_axis_is_named(monkeypatch):
    # Independent axes: a window may have settled its tense while its POV moves.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, VOICE_DRIFTING_DRAFT: ("second", "past")})
    calls = _forced_call(monkeypatch, DRIFTING_DRAFT)

    await _collect(_ctx(VOICE_DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    instruction = calls[0]["tail_messages"][2]["content"]
    assert "third person" in instruction
    assert "tense" not in instruction


async def test_consistent_voice_makes_no_llm_call(monkeypatch):
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, CONSISTENT_DRAFT: THIRD_PAST})
    calls = _forced_call(monkeypatch, "should not be used")

    events = await _collect(_ctx(CONSISTENT_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == []
    assert calls == []


async def test_an_unstable_baseline_voice_makes_no_llm_call(monkeypatch):
    # Two assistant messages disagreeing on both axes -> neither is enforced, and
    # the draft is never classified at all.
    _voice_on(monkeypatch)
    seen = _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, ASTERISK_MSG: SECOND_PRESENT})
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
    seen = _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
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
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})

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
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})

    async def boom(message_id, workflow_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(voice, "get_workflow_message_state", boom)

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_an_empty_rewrite_falls_through_to_the_markup_path(monkeypatch):
    # A forced call that fills nothing must not blank the reply.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
    _forced_call(monkeypatch, "")

    events = await _collect(_ctx(DRIFTING_DRAFT, [{"role": "assistant", "content": QUOTED_BASELINE}]))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]


async def test_a_cached_message_id_is_not_reclassified(monkeypatch):
    # Steady state is cache hits for the window and one classification of the
    # draft. History rows in production carry an id; the cache is keyed on it.
    _voice_on(monkeypatch)
    seen = _classifier(monkeypatch, {VOICE_DRIFTING_DRAFT: SECOND_PRESENT})
    _forced_call(monkeypatch, DRIFTING_DRAFT)

    async def cached(message_id, workflow_id):
        assert (message_id, workflow_id) == (7, "format_consistency")
        return {"pov": "third", "tense": "past"}

    async def no_write(message_id, workflow_id, payload):
        raise AssertionError("a cache hit must not write")

    monkeypatch.setattr(voice, "get_workflow_message_state", cached)
    monkeypatch.setattr(voice, "set_workflow_message_state", no_write)

    history = [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]
    events = await _collect(_ctx(VOICE_DRIFTING_DRAFT, history))

    assert events == [{"type": "draft_replaced", "draft": NORMALIZED}]
    assert seen == [VOICE_DRIFTING_DRAFT]  # the draft only; the window was cached


async def test_a_cache_miss_backfills_the_labels(monkeypatch):
    # History predating the toggle carries no labels, so the read backfills them.
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE: THIRD_PAST, CONSISTENT_DRAFT: THIRD_PAST})
    written: list[tuple] = []

    async def empty(message_id, workflow_id):
        return None

    async def record(message_id, workflow_id, payload):
        written.append((message_id, workflow_id, payload))

    monkeypatch.setattr(voice, "get_workflow_message_state", empty)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)

    history = [{"id": 7, "role": "assistant", "content": QUOTED_BASELINE}]
    await _collect(_ctx(CONSISTENT_DRAFT, history))

    assert written == [(7, "format_consistency", {"pov": "third", "tense": "past"})]
