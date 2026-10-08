"""The subject fixation edit inside the Editor pass: when it calls, what it sends, and what it may change."""

import json

import pytest

from backend.analysis.subjects import SUBJECT_DESCRIPTIONS, subject_pair_parts, subjects_input
from backend.inference import CachedBase, LLMClient, local_ml
from backend.pipeline import subject_tags
from backend.pipeline.passes.editor import editor_pass, subject_repeats
from backend.pipeline.subject_tags import TaggedReply
from backend.prompting.tool_catalog import enabled_schemas

SETTINGS = {"model_name": "test-model", "enable_agent": 1, "reasoning_enabled_passes": {}}
REQUEST = "Mara waits at the tavern."
DRAFT = '*Her copper braid gleams in the lamplight.* "Hello." *She waits.*'
HAIR = {"hair": [0.0, 0.0, 1.0]}
EARLIER = '*Her copper braid catches the light.* "Again?"'
OTHER = '*She shrugs.* "Fine."'
# Hair described in every other reply: a nominee for the comparer, short of a presence streak.
HISTORY = [TaggedReply(EARLIER, HAIR), TaggedReply(OTHER, {"hair": [1.0, 0.0, 0.0]})] * 4


@pytest.fixture(autouse=True)
def tagger(monkeypatch):
    seen: list[str] = []

    async def fake(narration: str):
        seen.append(narration)
        return {"hair": [0.0, 0.0, 1.0] if "copper" in narration else [1.0, 0.0, 0.0]}

    monkeypatch.setattr(local_ml, "aclassify_subjects", fake)
    monkeypatch.setattr(subject_tags, "_memo", {})
    return seen


class Comparer:
    def __init__(self, monkeypatch, repeat: float | Exception):
        self.pairs: list[tuple[str, str]] = []
        comparer = self

        async def compare(pairs):
            comparer.pairs.extend(pairs)
            if isinstance(repeat, Exception):
                raise repeat
            return [dict.fromkeys(local_ml.SUBJECT_CATEGORIES, repeat) for _ in pairs]

        monkeypatch.setattr(local_ml, "acompare_subjects", compare)


@pytest.fixture
def comparer(monkeypatch):
    return lambda repeat: Comparer(monkeypatch, repeat)


class Editor:
    def __init__(self, patches: list[tuple[str, str]], *, separate_calls: bool = False):
        self.client = LLMClient("http://localhost:9999")
        self.calls: list[dict] = []
        editor = self

        async def complete(*, messages, **kwargs):
            editor.calls.append({"messages": list(messages), **kwargs})
            groups = [[patch] for patch in patches] if separate_calls else [patches]
            yield {
                "type": "done",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"c{i}",
                            "function": {
                                "name": "editor_find_replace",
                                "arguments": json.dumps({"patches": [{"find": s, "replace": r} for s, r in group]}),
                            },
                        }
                        for i, group in enumerate(groups)
                    ],
                },
            }

        self.client.complete = complete  # type: ignore[method-assign]


def _base() -> CachedBase:
    return CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas({"editor_find_replace": True}, {})),
        model="test-model",
    )


async def _run(editor: Editor, draft: str = DRAFT, history=HISTORY) -> tuple[list[dict], dict]:
    events = [
        ev
        async for ev in editor_pass(
            editor.client,
            _base(),
            REQUEST,
            draft,
            SETTINGS,
            [],
            audit_enabled=False,
            subject_history=history,
        )
    ]
    return events, next(ev for ev in events if ev["type"] == "done")


@pytest.fixture(autouse=True)
def repeats(comparer):
    return comparer(0.9)


async def test_a_streak_gets_one_forced_exact_edit_on_the_writer_prefix(repeats, tagger):
    editor = Editor([("Her copper braid gleams in the lamplight.", "She looks up from the lamplight.")])
    events, done = await _run(editor)

    assert {"type": "step", "step": "subject_fixation"} in events
    assert done["draft"] == '*She looks up from the lamplight.* "Hello." *She waits.*'
    [call] = editor.calls
    assert call["tool_choice"] == {"type": "function", "function": {"name": "editor_find_replace"}}
    # The Writer's exact request and the draft are replayed, so the call extends the Writer's cached prefix.
    assert [m["content"] for m in call["messages"][1:3]] == [REQUEST, DRAFT]
    prompt = call["messages"][-1]["content"]
    assert prompt.splitlines()[-1] == f"- Descriptions of {SUBJECT_DESCRIPTIONS['hair']}]"
    # The comparer read the narration of each earlier reply against the draft's, one pair per reply, newest first.
    assert repeats.pairs == [subject_pair_parts(subjects_input(r.text), subjects_input(DRAFT)) for r in HISTORY]
    assert [c["name"] for c in done["tool_calls"]] == ["editor_find_replace"]
    # Persistence tags the final reply, after any later post-processing or secondary workflows.
    assert tagger == [subjects_input(DRAFT)]


async def test_no_cut_when_the_comparer_finds_no_repeat(comparer):
    comparer(0.2)
    editor = Editor([("copper braid gleams", "braid sways")])
    events, done = await _run(editor)
    assert editor.calls == [] and done["draft"] is None
    assert not any(ev.get("step") == "subject_fixation" for ev in events)


async def test_a_comparer_failure_is_reported_and_keeps_the_draft(comparer):
    comparer(RuntimeError("model unavailable"))
    editor = Editor([("copper braid gleams", "braid sways")])
    events, done = await _run(editor)
    assert [ev["during"] for ev in events if ev["type"] == "failure"] == ["subject_fixation"]
    assert editor.calls == [] and done["draft"] is None


async def test_a_stop_during_the_comparer_read_keeps_the_draft(monkeypatch):
    editor = Editor([("copper braid gleams", "braid sways")])

    async def compare(pairs):
        editor.client.abort()
        return [dict.fromkeys(local_ml.SUBJECT_CATEGORIES, 0.9) for _ in pairs]

    monkeypatch.setattr(local_ml, "acompare_subjects", compare)
    events, done = await _run(editor)
    assert editor.calls == [] and done["draft"] is None
    assert not any(ev.get("step") == "subject_fixation" for ev in events)


async def test_a_reply_without_narration_is_never_read_and_never_counts(repeats):
    spoken = '"Again? Fine."'
    scores = await subject_repeats.repeat_scores(DRAFT, [EARLIER, spoken, EARLIER], ["hair"])
    assert scores == {"hair": [0.9, None, 0.9]}
    assert repeats.pairs == [subject_pair_parts(subjects_input(EARLIER), subjects_input(DRAFT))] * 2


async def test_a_subject_in_every_recent_reply_is_cut_without_the_comparer(repeats):
    editor = Editor([("Her copper braid gleams in the lamplight.", "She looks up from the lamplight.")])
    history = [TaggedReply('*She tugs her braid.* "Again?"', {"hair": [0.0, 1.0, 0.0]})] * 4
    events, done = await _run(editor, history=history)
    assert {"type": "step", "step": "subject_fixation"} in events and len(editor.calls) == 1
    assert done["draft"] == '*She looks up from the lamplight.* "Hello." *She waits.*' and repeats.pairs == []
    prompt = editor.calls[0]["messages"][-1]["content"]
    assert prompt.splitlines()[-1] == f"- Any mention of {SUBJECT_DESCRIPTIONS['hair']}]"


async def test_mixed_flags_get_their_own_editing_rules_in_one_call(monkeypatch, repeats):
    async def classify(narration: str):
        return {
            "hair": [0.0, 1.0, 0.0] if "braid" in narration else [1.0, 0.0, 0.0],
            "eyes": [0.0, 0.0, 1.0] if "eyes" in narration else [1.0, 0.0, 0.0],
        }

    monkeypatch.setattr(local_ml, "aclassify_subjects", classify)
    history = [
        TaggedReply("*She tugs her braid. Her violet eyes shine.*", {"hair": [0.0, 1.0, 0.0], "eyes": [0.0, 0.0, 1.0]}),
        TaggedReply("*She tugs her braid.*", {"hair": [0.0, 1.0, 0.0], "eyes": [1.0, 0.0, 0.0]}),
    ] * 2
    draft = '*She tugs her braid. Her violet eyes gleam as she opens the door.* "Hello."'
    editor = Editor([("She tugs her braid. ", ""), ("Her violet eyes gleam as she opens the door.", "She opens the door.")])
    _, done = await _run(editor, draft=draft, history=history)

    assert done["draft"] == '*She opens the door.* "Hello."'
    [call] = editor.calls
    prompt = call["messages"][-1]["content"]
    targets = {line.rstrip("]") for line in prompt.splitlines() if line.startswith("- ")}
    assert {f"- Descriptions of {SUBJECT_DESCRIPTIONS['eyes']}", f"- Any mention of {SUBJECT_DESCRIPTIONS['hair']}"} <= targets


async def test_editor_can_leave_an_essential_recurring_action_unchanged(monkeypatch, repeats):
    async def classify(narration: str):
        return {"hands": [0.0, 1.0, 0.0]}

    monkeypatch.setattr(local_ml, "aclassify_subjects", classify)
    history = [TaggedReply("*She holds his hand.*", {"hands": [0.0, 1.0, 0.0]})] * 4
    draft = '*She catches his hand before he can strike.* "Stop."'
    editor = Editor([])
    events, done = await _run(editor, draft=draft, history=history)

    assert done["draft"] is None and repeats.pairs == []
    [call] = editor.calls
    assert not any(ev["type"] == "draft_update" for ev in events)


async def test_a_patch_that_reaches_into_dialogue_is_skipped():
    editor = Editor([('"Hello."', '"Hi."'), ("copper braid gleams", "braid sways")])
    _, done = await _run(editor)
    assert done["draft"] == '*Her braid sways in the lamplight.* "Hello." *She waits.*'


async def test_no_streak_means_no_call(repeats):
    editor = Editor([])
    events, done = await _run(editor, draft="*She waits by the fire.*")
    assert editor.calls == [] and repeats.pairs == []
    assert not any(ev.get("step") == "subject_fixation" for ev in events)
    assert done["draft"] is None


async def test_without_history_tags_the_draft_is_never_tagged(tagger):
    editor = Editor([])
    await _run(editor, history=None)
    assert tagger == [] and editor.calls == []


async def test_in_bare_dialogue_only_the_asterisk_narration_may_change():
    draft = "\n\n".join(
        [
            "*Her copper braid gleams as she leans across the counter, fingers drumming on the wood.*",
            "Well, look who finally came crawling back. I was starting to think you'd forgotten me.",
            "*She tilts her head, a smile tugging at her lips.*",
            "Your hair always gave you away, you know. Even now.",
        ]
    )
    editor = Editor(
        [("Your hair always gave you away", "You always gave yourself away"), ("copper braid gleams", "braid sways")]
    )
    _, done = await _run(editor, draft=draft)
    assert done["draft"] == draft.replace("copper braid gleams", "braid sways")


@pytest.mark.parametrize("find", ["Her copper braid gleams.", "*Her copper braid gleams.*"])
@pytest.mark.parametrize("separate_calls", [False, True])
async def test_deleting_the_only_narration_beat_keeps_bare_dialogue_protected(find, separate_calls):
    dialogue = "Your hair always gave you away."
    draft = f"*Her copper braid gleams.*\n\n{dialogue}"
    editor = Editor([(find, ""), (dialogue, "You gave yourself away.")], separate_calls=separate_calls)
    _, done = await _run(editor, draft=draft)
    assert done["draft"] == dialogue


@pytest.mark.parametrize("dialogue", ["hello there.", '"hello there."'])
async def test_deletion_healing_cannot_change_dialogue_and_later_safe_patches_still_apply(dialogue):
    draft = f"*Her copper braid gleams.*\n\n{dialogue}\n\n*She waits.*"
    editor = Editor([("*Her copper braid gleams.*", ""), ("She waits.", "She leaves.")])
    _, done = await _run(editor, draft=draft)
    assert done["draft"] == draft.replace("She waits.", "She leaves.")


async def test_a_tagger_failure_is_reported_and_keeps_the_draft(monkeypatch):
    async def broken(narration: str):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(local_ml, "aclassify_subjects", broken)
    editor = Editor([])
    events, done = await _run(editor)
    assert [ev["during"] for ev in events if ev["type"] == "failure"] == ["subject_fixation"]
    assert editor.calls == [] and done["draft"] is None
