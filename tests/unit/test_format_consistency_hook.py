import hashlib
import json
from types import MappingProxyType

import pytest

from backend.analysis.text.markup import classify_axes, narration_only
from backend.analysis.text.roleplay import AxisStyle, Dialogue, Narration
from backend.inference import local_ml
from backend.inference.local_models import assets, dependencies
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
from backend.workflows.format_consistency.normalization import baseline_axes

QUOTED_BASELINE = 'She smiles. "Hello there," she says warmly.'
QUOTED_BASELINE_NARRATION = "She smiles. she says warmly."
DRIFTING_DRAFT = "*She steps closer, watching him carefully.* Are you sure about this?"
NORMALIZED = 'She steps closer, watching him carefully. "Are you sure about this?"'
CONSISTENT_DRAFT = 'He nods slowly. "I understand," he replies.'
CONSISTENT_NARRATION = "He nods slowly. he replies."
ASTERISK_MSG = "*She smiles and steps back, turning to the window.* I won't go."
BASELINE = [{"role": "assistant", "content": QUOTED_BASELINE}]
REPLACED = [{"type": "draft_replaced", "draft": NORMALIZED}]
UNUSED = "should not be used"

WRITER_CLIENT = object()
AGENT_CLIENT = object()


def _ctx(draft: str, history: list[dict], settings: dict | None = None) -> PostCtx:
    return PostCtx(
        conversation_id="c1",
        history=tuple(MappingProxyType(m) for m in history),
        draft=draft,
        effective_msg="and then?",
        director_output=MappingProxyType({}),
        settings=MappingProxyType(settings or {}),
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
    """Keep markup-only tests independent of the local classifiers and database.

    The POV model is gated in the hook itself. The markup classifier is gated
    inside the toolkit, so it is kept off where readiness starts: no model on disk.
    """
    monkeypatch.setattr(hooks, "local_feature_ready", lambda feature, settings: False)
    monkeypatch.setattr(assets, "present", lambda feature: False)


async def _collect(ctx, *, include_status: bool = False) -> list[dict]:
    events = [ev async for ev in hooks.post_pipeline(ctx)]
    return events if include_status else [ev for ev in events if ev.get("event") != "phase_status"]


async def _labels(msg) -> tuple[str, str] | None:
    """A history row's voice labels, under its heuristic markup reading."""
    return await voice.labels_for(msg, classify_axes(msg["content"]))


def _raising(exc: Exception):
    def boom(*args, **kwargs):
        raise exc

    async def aboom(*args, **kwargs):
        raise exc

    return boom, aboom


@pytest.mark.parametrize(
    "draft,history,expected",
    [
        (DRIFTING_DRAFT, [*BASELINE, {"role": "user", "content": "and then?"}], REPLACED),
        # An unstable baseline, an already-consistent draft, and no assistant baseline all leave the draft alone.
        ('She frowns. "What now?"', [*BASELINE, {"role": "assistant", "content": ASTERISK_MSG}], []),
        (CONSISTENT_DRAFT, BASELINE, []),
        (DRIFTING_DRAFT, [{"role": "user", "content": "hello"}], []),
    ],
)
async def test_markup_drift_is_normalized_only_against_a_stable_baseline(draft, history, expected):
    assert await _collect(_ctx(draft, history)) == expected


async def test_reports_format_check_progress_to_the_turn_status(monkeypatch):
    async def voice_enabled(_ctx):
        return True

    async def hold_voice(_ctx, text, _window, _styles):
        return text

    monkeypatch.setattr(hooks, "_voice_enabled", voice_enabled)
    monkeypatch.setattr(hooks, "_hold_voice", hold_voice)
    assert await _collect(_ctx(CONSISTENT_DRAFT, BASELINE), include_status=True) == [
        {"event": "phase_status", "data": {"channel": "workflow:format_consistency", "label": "Matching voice and format…"}},
        {"event": "phase_status", "data": {"channel": "workflow:format_consistency", "state": "done"}},
    ]


async def test_the_aggregate_convention_only_reaches_the_markup_target(monkeypatch):
    _voice_on(monkeypatch)
    convention = AxisStyle(Dialogue.QUOTED, Narration.BARE)
    votes: list[list[AxisStyle]] = []

    def fake_vote(styles):
        votes.append(list(styles))
        return convention

    async def fake_hold(ctx, text, window, styles):
        return text

    class Unchanged:
        changed = False

    def fake_normalize(draft, messages, *, enabled, target, source):
        assert enabled is True
        assert target is convention
        assert source == classify_axes(draft)
        return draft, Unchanged()

    monkeypatch.setattr(hooks, "vote_axes", fake_vote)
    monkeypatch.setattr(hooks, "_hold_voice", fake_hold)
    monkeypatch.setattr(hooks, "normalize_to_baseline", fake_normalize)
    assert await _collect(_ctx(CONSISTENT_DRAFT, BASELINE)) == []
    assert votes == [[classify_axes(QUOTED_BASELINE)]]


# ---------- the voice half ----------

THIRD_PAST = ("third", "past")
SECOND_PRESENT = ("second", "present")
THIRD_PRESENT = ("third", "present")

VOICE_DRIFTING_DRAFT = 'You step closer, watching him carefully. "Are you sure about this?"'
VOICE_DRIFTING_NARRATION = "You step closer, watching him carefully."
VOICE_DRIFT = {QUOTED_BASELINE_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT}


def _voice_on(monkeypatch, *, enabled: bool = True):
    """Model present and the opt-in config set."""
    monkeypatch.setattr(hooks, "local_feature_ready", lambda feature, settings: True)

    async def fake_config(workflow_id):
        return {"voice_consistency": enabled}

    monkeypatch.setattr(hooks, "get_workflow_config", fake_config)


def _classifier(
    monkeypatch, answers: dict[str, tuple[str, str]], chunks: dict[str, list[tuple[str, str]]] | None = None
) -> list[str]:
    """Read each narration as the windows in *chunks*, else as one window answered
    from *answers*; record every text the classifier was shown."""
    seen: list[str] = []

    async def fake_chunks(text: str) -> list[tuple[str, str]]:
        seen.append(text)
        if chunks is not None and text in chunks:
            return chunks[text]
        return [answers.get(text, ("ambiguous", "ambiguous"))]

    monkeypatch.setattr(voice, "classify_pov_tense_chunks", fake_chunks)
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


async def _voice_run(monkeypatch, draft, answers, chunks=None, rewritten=UNUSED, history=BASELINE, ctx=None):
    """Voice check on, classifier answering *answers*/*chunks*, rewrite answering *rewritten*."""
    _voice_on(monkeypatch)
    seen = _classifier(monkeypatch, answers, chunks)
    calls = _forced_call(monkeypatch, rewritten)
    return await _collect(ctx or _ctx(draft, history)), calls, seen


def _cache(monkeypatch, stored: dict | None, *, writes: bool = True) -> list[dict]:
    """Serve *stored* as the row's cached labels and record every write (or refuse writes)."""
    written: list[dict] = []

    async def cached(message_id, workflow_id):
        assert (message_id, workflow_id) == (7, "format_consistency")
        return stored

    async def record(message_id, workflow_id, payload):
        if not writes:
            raise AssertionError("this read must not write the label cache")
        written.append(payload)

    monkeypatch.setattr(voice, "get_workflow_message_state", cached)
    monkeypatch.setattr(voice, "set_workflow_message_state", record)
    return written


def _labels_payload(content: str, pov="third", tense="past", **extra) -> dict:
    return {
        "pov": pov,
        "tense": tense,
        "dialogue": "quoted",
        "content_sha256": voice._content_digest(content),
        "classifier": voice.local_model_identity(voice.FEATURE),
        **extra,
    }


def test_voice_rewrite_declares_its_own_compatible_standalone_schema():
    function = VOICE_REWRITE_TOOL.schema["function"]
    assert VOICE_REWRITE_TOOL.name == VOICE_REWRITE_TOOL_NAME
    assert VOICE_REWRITE_TOOL.standalone is True
    assert function["name"] == VOICE_REWRITE_TOOL_NAME
    assert VOICE_REWRITE_LENGTH_RULE in function["description"]
    assert VOICE_REWRITE_LENGTH_RULE in hooks._SYSTEM
    assert "audit" not in function["description"].lower()
    assert "length constraint" not in function["description"].lower()


async def test_the_rewrite_is_a_self_contained_call_on_the_agent_lane(monkeypatch):
    ctx = _ctx(VOICE_DRIFTING_DRAFT, BASELINE)
    _, [call], _ = await _voice_run(monkeypatch, None, VOICE_DRIFT, rewritten=DRIFTING_DRAFT, ctx=ctx)
    assert call["client"] is AGENT_CLIENT
    assert call["model_name"] == "agent-model"
    assert call["tool_name"] == VOICE_REWRITE_TOOL_NAME
    assert call["enabled_tools"] is None
    assert call["cache_shape"] == "format_consistency:voice_rewrite"
    assert call["prefix"] != ctx.prefix
    assert [m["role"] for m in call["prefix"]] == ["system"]

    [tail] = call["tail_messages"]
    assert tail["role"] == "user"
    assert VOICE_DRIFTING_DRAFT in tail["content"]
    # The newest in-voice reply is the one reference, ahead of the passage.
    assert tail["content"].count(QUOTED_BASELINE) == 1
    assert tail["content"].index(QUOTED_BASELINE) < tail["content"].index(VOICE_DRIFTING_DRAFT)
    assert ctx.effective_msg not in tail["content"]


@pytest.mark.parametrize(
    "draft,answers,chunks,present,absent",
    [
        # Only the drifting axis is named.
        (
            VOICE_DRIFTING_DRAFT,
            {**VOICE_DRIFT, VOICE_DRIFTING_NARRATION: ("second", "past")},
            None,
            ["third person"],
            ["tense"],
        ),
        # `second` is the "He tells you" register: a bare "second person" instruction would make the narration's subject "you".
        (
            CONSISTENT_DRAFT,
            {QUOTED_BASELINE_NARRATION: SECOND_PRESENT, CONSISTENT_NARRATION: ("first", "present")},
            None,
            ["third person for the speaking character"],
            ["second person"],
        ),
        # A draft that never addresses "you" still drifts from `second`.
        (
            CONSISTENT_DRAFT,
            {QUOTED_BASELINE_NARRATION: SECOND_PRESENT},
            {CONSISTENT_NARRATION: [("third", "present"), ("ambiguous", "present"), ("third", "present")]},
            ["third person for the speaking character"],
            [],
        ),
        # Precedence runs one way: one `second` window drifts from third whatever the rest reads.
        (
            VOICE_DRIFTING_DRAFT,
            {QUOTED_BASELINE_NARRATION: THIRD_PAST},
            {VOICE_DRIFTING_NARRATION: [("second", "past"), ("third", "past"), ("third", "past")]},
            ["third person throughout"],
            [],
        ),
        # Windows combine each axis on its own.
        (
            CONSISTENT_DRAFT,
            {QUOTED_BASELINE_NARRATION: SECOND_PRESENT},
            {CONSISTENT_NARRATION: [("third", "past"), ("second", "past")]},
            ["present tense"],
            ["person"],
        ),
    ],
)
async def test_the_rewrite_instruction_names_only_the_target_voice(monkeypatch, draft, answers, chunks, present, absent):
    _, [call], _ = await _voice_run(monkeypatch, draft, answers, chunks, rewritten=draft)
    instruction = call["tail_messages"][0]["content"]
    assert all(phrase in instruction for phrase in present)
    assert not any(phrase in instruction for phrase in absent)


def test_no_pov_instruction_invites_a_name_the_passage_lacks():
    """The system rules forbid introducing a name, so no target may ask for one."""
    assert "name" in hooks._SYSTEM
    for phrase in voice._POV_PHRASE.values():
        assert "name" not in phrase


@pytest.mark.parametrize(
    "draft,answers,chunks",
    [
        (CONSISTENT_DRAFT, {QUOTED_BASELINE_NARRATION: THIRD_PAST, CONSISTENT_NARRATION: THIRD_PAST}, None),
        # The label is a precedence rule: one "you" anywhere in the narration makes a draft (or a baseline row) `second`.
        (
            CONSISTENT_DRAFT,
            {QUOTED_BASELINE_NARRATION: SECOND_PRESENT},
            {CONSISTENT_NARRATION: [("third", "present"), ("second", "present"), ("third", "present")]},
        ),
        (
            VOICE_DRIFTING_DRAFT,
            {VOICE_DRIFTING_NARRATION: ("second", "past")},
            {QUOTED_BASELINE_NARRATION: [("third", "past"), ("second", "past"), ("third", "past")]},
        ),
        # Inner monologue reads `first` in one window of a deep-third reply; it must not vote the baseline into first person.
        (
            VOICE_DRIFTING_DRAFT,
            {VOICE_DRIFTING_NARRATION: THIRD_PRESENT},
            {QUOTED_BASELINE_NARRATION: [THIRD_PRESENT] * 3 + [("first", "present"), THIRD_PRESENT] * 2},
        ),
        # A tense flip in one window is not drift.
        (
            CONSISTENT_DRAFT,
            {QUOTED_BASELINE_NARRATION: THIRD_PAST},
            {CONSISTENT_NARRATION: [("third", "present"), ("third", "past"), ("third", "past")]},
        ),
    ],
)
async def test_a_matching_voice_makes_no_llm_call(monkeypatch, draft, answers, chunks):
    events, calls, _ = await _voice_run(monkeypatch, draft, answers, chunks)
    assert events == []
    assert calls == []


async def test_an_unstable_baseline_voice_makes_no_llm_call(monkeypatch):
    history = [*BASELINE, {"role": "assistant", "content": ASTERISK_MSG}]
    answers = {QUOTED_BASELINE_NARRATION: THIRD_PAST, ASTERISK_MSG: SECOND_PRESENT}
    _, calls, seen = await _voice_run(monkeypatch, CONSISTENT_DRAFT, answers, history=history)
    assert calls == []
    assert CONSISTENT_DRAFT not in seen


async def test_config_off_classifies_nothing(monkeypatch):
    _voice_on(monkeypatch, enabled=False)
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, UNUSED)
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == REPLACED
    assert seen == []
    assert calls == []


async def test_classifier_absent_never_reads_the_config_slot(monkeypatch):
    _, boom = _raising(AssertionError("the config slot must not be read without the classifier"))
    monkeypatch.setattr(hooks, "get_workflow_config", boom)
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == REPLACED


@pytest.mark.parametrize("failure", ["classifier", "forced_call", "config_slot", "label_cache", "empty_rewrite"])
async def test_a_failing_voice_step_still_normalizes_markup(monkeypatch, failure):
    sync_boom, async_boom = _raising(RuntimeError(failure))
    _voice_on(monkeypatch)
    _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST, DRIFTING_DRAFT: SECOND_PRESENT})
    calls = _forced_call(monkeypatch, "" if failure == "empty_rewrite" else UNUSED)
    target, name, fake = {
        "classifier": (voice, "classify_pov_tense_chunks", async_boom),
        "forced_call": (hooks, "forced_tool_call", sync_boom),
        "config_slot": (hooks, "get_workflow_config", async_boom),
        "label_cache": (voice, "get_workflow_message_state", async_boom),
    }.get(failure, (None, "", None))
    if target is not None:
        monkeypatch.setattr(target, name, fake)

    history = [{"id": 7, **BASELINE[0]}] if failure == "label_cache" else BASELINE
    assert await _collect(_ctx(DRIFTING_DRAFT, history)) == REPLACED
    if failure == "classifier":
        assert calls == []


async def test_a_cached_message_id_is_not_reclassified(monkeypatch):
    _cache(monkeypatch, _labels_payload(QUOTED_BASELINE), writes=False)
    events, _, seen = await _voice_run(
        monkeypatch,
        VOICE_DRIFTING_DRAFT,
        {VOICE_DRIFTING_NARRATION: SECOND_PRESENT},
        rewritten=DRIFTING_DRAFT,
        history=[{"id": 7, **BASELINE[0]}],
    )
    assert events == REPLACED
    assert seen == [VOICE_DRIFTING_NARRATION]


async def test_a_cache_miss_backfills_the_labels(monkeypatch):
    written = _cache(monkeypatch, None)
    answers = {QUOTED_BASELINE_NARRATION: THIRD_PAST, CONSISTENT_NARRATION: THIRD_PAST}
    await _voice_run(monkeypatch, CONSISTENT_DRAFT, answers, history=[{"id": 7, **BASELINE[0]}])
    assert written == [_labels_payload(QUOTED_BASELINE)]


async def test_bare_dialogue_is_removed_before_voice_classification(monkeypatch):
    """Classify only the narration from bare-dialogue messages."""
    baseline = (
        "As president of the Literature Club, it's my duty to make the club fun and "
        "exciting for everyone! *Heidi smiles kindly at you.* Tell me, what brings you here today?"
    )
    draft = "Welcome to the club. *Heidi waits by the desk.* Please, take a seat."
    answers = {"Heidi smiles kindly at you.": THIRD_PRESENT, "Heidi waits by the desk.": THIRD_PRESENT}
    events, calls, seen = await _voice_run(monkeypatch, draft, answers, history=[{"role": "assistant", "content": baseline}])
    assert events == []
    assert calls == []
    assert seen == ["Heidi smiles kindly at you.", "Heidi waits by the desk."]


@pytest.mark.parametrize("cached_dialogue", [None, "quoted"])
async def test_labels_are_reclassified_when_the_cached_convention_differs(monkeypatch, cached_dialogue):
    msg = {"id": 7, "role": "assistant", "content": "Stay with me. *Heidi waits by the desk.* We can talk here."}
    stored = _labels_payload(msg["content"], "second", "present", other="preserved")
    if cached_dialogue is None:
        del stored["dialogue"]
    seen = _classifier(monkeypatch, {"Heidi waits by the desk.": THIRD_PAST})
    written = _cache(monkeypatch, stored)

    assert classify_axes(msg["content"]).dialogue == Dialogue.BARE
    assert await _labels(msg) == THIRD_PAST
    assert seen == ["Heidi waits by the desk."]
    assert written == [{**_labels_payload(msg["content"], other="preserved"), "dialogue": "bare"}]


@pytest.mark.parametrize(
    "stale",
    [
        _labels_payload("You wait by the door.", "second", "present"),  # the message content changed
        {**_labels_payload(QUOTED_BASELINE, "second", "present", other="preserved"), "classifier": None},
        {
            **_labels_payload(QUOTED_BASELINE, "second", "present", other="preserved"),
            "classifier": "chartreuse-verte/ettin-povtense-17m@old-revision",
        },
    ],
)
async def test_stale_labels_are_reclassified(monkeypatch, stale):
    seen = _classifier(monkeypatch, {QUOTED_BASELINE_NARRATION: THIRD_PAST})
    written = _cache(monkeypatch, stale)
    assert await _labels({"id": 7, "role": "assistant", "content": QUOTED_BASELINE}) == THIRD_PAST
    assert seen == [QUOTED_BASELINE_NARRATION]
    assert written[0]["content_sha256"] == voice._content_digest(QUOTED_BASELINE)
    assert written[0]["classifier"] == voice.local_model_identity(voice.FEATURE)
    assert written[0].get("other") == stale.get("other")


@pytest.mark.parametrize("policy", [b"", b"narration-v2\0"])
async def test_cached_labels_are_refreshed_after_the_reading_policy_changes(monkeypatch, policy):
    """Labels from an older extraction policy, or from tail-only reads, are stale."""
    text = "*You can do this. You have to keep moving.* she thinks, waiting."
    seen = _classifier(monkeypatch, {"she thinks, waiting.": THIRD_PRESENT})
    written = _cache(
        monkeypatch,
        {
            "pov": "ambiguous",
            "tense": "ambiguous",
            "dialogue": classify_axes(text).dialogue.value,
            "classifier": voice.local_model_identity(voice.FEATURE),
            "content_sha256": hashlib.sha256(policy + text.encode()).hexdigest(),
        },
    )
    assert await _labels({"id": 7, "content": text}) == THIRD_PRESENT
    assert seen == ["she thinks, waiting."]
    assert written[0]["content_sha256"] == voice._content_digest(text)


async def test_classifier_failure_is_not_cached(monkeypatch):
    _cache(monkeypatch, None, writes=False)
    monkeypatch.setattr(voice, "classify_pov_tense_chunks", _raising(RuntimeError("model failed to load"))[1])
    assert await _labels({"id": 7, "role": "assistant", "content": QUOTED_BASELINE}) is None


# ---------- source parsing: every text under its own convention ----------


WINDOW_NARRATION = "She smiles, stepping back toward the window."
BARE_ROW = f"*{WINDOW_NARRATION}* Hello there."


async def test_a_quoted_draft_is_parsed_as_quoted_in_a_bare_dialogue_chat(monkeypatch):
    baseline = BARE_ROW
    draft = 'You step closer, watching him. "Are you sure about this?"'
    draft_narration = "You step closer, watching him."
    assert baseline_axes([baseline]).dialogue == Dialogue.BARE
    assert narration_only(draft, Dialogue.BARE) == ""  # what the old code passed on

    _, calls, seen = await _voice_run(
        monkeypatch,
        draft,
        {WINDOW_NARRATION: THIRD_PAST, draft_narration: SECOND_PRESENT},
        rewritten="*She steps closer, watching him.* Are you sure about this?",
        history=[{"role": "assistant", "content": baseline}],
    )
    assert seen == [WINDOW_NARRATION, draft_narration]
    assert len(calls) == 1  # the drift was seen, not swallowed


async def test_a_bare_dialogue_draft_keeps_its_speech_out_of_the_classifier(monkeypatch):
    draft = "*She waits by the desk.* Tell me, what brings you here today?"
    assert classify_axes(draft).dialogue == Dialogue.BARE
    answers = {QUOTED_BASELINE_NARRATION: THIRD_PAST, "She waits by the desk.": THIRD_PAST}
    _, calls, seen = await _voice_run(monkeypatch, draft, answers)
    assert seen == [QUOTED_BASELINE_NARRATION, "She waits by the desk."]
    assert calls == []  # both ends third/past: no drift, and no speech voted


async def test_each_history_row_is_classified_under_its_own_convention(monkeypatch):
    assert classify_axes(BARE_ROW).dialogue == Dialogue.BARE
    assert classify_axes(CONSISTENT_DRAFT).dialogue == Dialogue.QUOTED

    _, _, seen = await _voice_run(
        monkeypatch,
        VOICE_DRIFTING_DRAFT,
        {WINDOW_NARRATION: THIRD_PAST, CONSISTENT_NARRATION: THIRD_PAST, VOICE_DRIFTING_NARRATION: SECOND_PRESENT},
        rewritten=CONSISTENT_DRAFT,
        history=[{"role": "assistant", "content": CONSISTENT_DRAFT}, {"role": "assistant", "content": BARE_ROW}],
    )
    assert seen == [WINDOW_NARRATION, CONSISTENT_NARRATION, VOICE_DRIFTING_NARRATION]


async def test_a_changed_window_majority_does_not_invalidate_a_cached_row(monkeypatch):
    _voice_on(monkeypatch)
    _cache(monkeypatch, _labels_payload(QUOTED_BASELINE), writes=False)
    seen = _classifier(monkeypatch, {})
    for neighbour in (CONSISTENT_DRAFT, ASTERISK_MSG):
        assert await _labels({"id": 7, "role": "assistant", "content": QUOTED_BASELINE}) == THIRD_PAST
        assert baseline_axes([QUOTED_BASELINE, neighbour]) is not None
    assert seen == []


# ---------- the rewrite is not trusted on sight ----------


@pytest.mark.parametrize(
    "rewritten,expected",
    [
        # A rewrite that changes the dialogue is discarded.
        ("*She steps closer, watching him carefully.* Are you certain about this?", []),
        # The Editor may have just cut this draft for the length guard; a rewrite that regrows it is discarded.
        (
            "She steps closer, watching him with great care and no small amount of worry, "
            'and after a long moment she finally speaks. "Are you sure about this?"',
            [],
        ),
        # A faithful rewrite is accepted even when its markup moved, and voice and markup drift compose into one event.
        (DRIFTING_DRAFT, REPLACED),
        # A schema-less lane can echo the call syntax into the argument.
        (f'{VOICE_REWRITE_TOOL_NAME}("{DRIFTING_DRAFT}")', REPLACED),
    ],
)
async def test_the_rewrite_is_checked_before_it_is_trusted(monkeypatch, rewritten, expected):
    events, calls, _ = await _voice_run(monkeypatch, VOICE_DRIFTING_DRAFT, VOICE_DRIFT, rewritten=rewritten)
    assert events == expected
    assert len(calls) == 1


def test_the_story_guard_rejects_changed_dialogue():
    assert guard.rejection(VOICE_DRIFTING_DRAFT, "*She steps closer.* Are you certain about this?")


# ---------- opt-in capture of the normalizer's exact inputs ----------


async def test_markup_capture_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv(capture.ENV, raising=False)
    written: list[str] = []
    monkeypatch.setattr(capture, "_append", lambda path, line: written.append(line))
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == REPLACED
    assert written == []


async def test_markup_capture_records_the_draft_the_normalizer_received(monkeypatch, tmp_path):
    path = tmp_path / "capture.jsonl"
    monkeypatch.setenv(capture.ENV, str(path))
    await _collect(_ctx(DRIFTING_DRAFT, [{"id": 3, **BASELINE[0]}, {"id": 4, "role": "user", "content": "and then?"}]))

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
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == REPLACED


# ---------- the markup classifier reads convention when it is installed ----------


def _markup_model(monkeypatch, answers: dict[str, tuple[str, str]]) -> list[str]:
    """Install the markup classifier, answering (narration, dialogue) from *answers*; record every text it read."""
    seen: list[str] = []

    async def fake(text: str) -> tuple[str, str]:
        seen.append(text)
        return answers[text]

    monkeypatch.setattr(assets, "present", lambda feature: True)
    monkeypatch.setattr(dependencies, "deps_ok", lambda feature=None: (True, ""))
    monkeypatch.setattr(local_ml, "aclassify_markup", fake)
    return seen


async def test_the_markup_classifier_decides_both_ends_of_the_rewrite(monkeypatch):
    seen = _markup_model(monkeypatch, {QUOTED_BASELINE: ("bare", "quoted"), DRIFTING_DRAFT: ("asterisk", "quoted")})
    # Read as quoted-dialogue prose, the draft's bare run is narration and stays unquoted.
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == [
        {"type": "draft_replaced", "draft": "She steps closer, watching him carefully. Are you sure about this?"}
    ]
    assert seen == [QUOTED_BASELINE, DRIFTING_DRAFT]


async def test_a_window_the_markup_classifier_cannot_read_leaves_the_draft_alone(monkeypatch):
    _markup_model(monkeypatch, {QUOTED_BASELINE: ("unknown", "unknown"), DRIFTING_DRAFT: ("asterisk", "bare")})
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == []


async def test_a_failing_or_disabled_markup_classifier_falls_back_to_the_heuristic(monkeypatch):
    seen = _markup_model(monkeypatch, {})
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE, {"local_ml_enabled": {"markup_classifier": False}})) == REPLACED
    assert seen == []

    monkeypatch.setattr(local_ml, "aclassify_markup", _raising(RuntimeError("failed to load: wrong head?"))[1])
    assert await _collect(_ctx(DRIFTING_DRAFT, BASELINE)) == REPLACED


async def test_one_markup_reading_per_window_row_serves_both_halves(monkeypatch):
    """The voice check extracts narration under the markup classifier's reading; the row is read once for both halves."""
    row = "Stay with me. *Heidi waits by the desk.* We can talk here."
    assert classify_axes(row).dialogue == Dialogue.BARE
    seen = _markup_model(monkeypatch, {row: ("bare", "quoted"), CONSISTENT_DRAFT: ("bare", "quoted")})
    narration = narration_only(row, Dialogue.QUOTED)
    _, _, voiced = await _voice_run(
        monkeypatch,
        CONSISTENT_DRAFT,
        {narration: THIRD_PAST, CONSISTENT_NARRATION: THIRD_PAST},
        history=[{"role": "assistant", "content": row}],
    )
    assert voiced[0] == narration
    assert narration != narration_only(row, Dialogue.BARE)
    assert seen.count(row) == 1
