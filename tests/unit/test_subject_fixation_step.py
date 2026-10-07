"""The subject fixation edit inside the Editor pass: when it calls, what it sends, and what it may change."""

import json

import pytest

from backend.inference import CachedBase, LLMClient, local_ml
from backend.pipeline import subject_tags
from backend.pipeline.passes.editor import editor_pass
from backend.prompting.tool_catalog import enabled_schemas

SETTINGS = {"model_name": "test-model", "enable_agent": 1, "reasoning_enabled_passes": {}}
REQUEST = "Mara waits at the tavern."
DRAFT = '*Her violet eyes glint in the lamplight.* "Hello." *She waits.*'
EYES = {"eyes": [0.0, 0.0, 1.0]}
HISTORY = [EYES] * 6  # eyes described in every one of the last six replies


@pytest.fixture(autouse=True)
def tagger(monkeypatch):
    seen: list[str] = []

    async def fake(narration: str):
        seen.append(narration)
        return {"eyes": [0.0, 0.0, 1.0] if "violet" in narration else [1.0, 0.0, 0.0]}

    monkeypatch.setattr(local_ml, "aclassify_subjects", fake)
    monkeypatch.setattr(subject_tags, "_memo", {})
    return seen


class Editor:
    def __init__(self, patches: list[tuple[str, str]]):
        self.client = LLMClient("http://localhost:9999")
        self.calls: list[dict] = []
        editor = self

        async def complete(*, messages, **kwargs):
            editor.calls.append({"messages": list(messages), **kwargs})
            arguments = json.dumps({"patches": [{"search": s, "replace": r} for s, r in patches]})
            yield {
                "type": "done",
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "c", "function": {"name": "editor_search_replace", "arguments": arguments}}],
                },
            }

        self.client.complete = complete  # type: ignore[method-assign]


def _base() -> CachedBase:
    return CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas({"editor_search_replace": True}, {})),
        model="test-model",
    )


async def _run(editor: Editor, draft: str = DRAFT, history=HISTORY) -> tuple[list[dict], dict]:
    events = [
        ev
        async for ev in editor_pass(
            editor.client, _base(), REQUEST, draft, SETTINGS, [], audit_enabled=False, subject_history_tags=history
        )
    ]
    return events, next(ev for ev in events if ev["type"] == "done")


async def test_a_streak_gets_one_forced_exact_edit_on_the_writer_prefix():
    editor = Editor([("Her violet eyes glint in the lamplight.", "She looks up from the lamplight.")])
    events, done = await _run(editor)

    assert {"type": "step", "step": "subject_fixation"} in events
    assert done["draft"] == '*She looks up from the lamplight.* "Hello." *She waits.*'
    [call] = editor.calls
    assert call["tool_choice"] == {"type": "function", "function": {"name": "editor_search_replace"}}
    # The Writer's exact request and the draft are replayed, so the call extends the Writer's cached prefix.
    assert [m["content"] for m in call["messages"][1:3]] == [REQUEST, DRAFT]


async def test_a_patch_that_reaches_into_dialogue_is_skipped():
    editor = Editor([('"Hello."', '"Hi."'), ("violet eyes glint", "gaze drifts")])
    _, done = await _run(editor)
    assert done["draft"] == '*Her gaze drifts in the lamplight.* "Hello." *She waits.*'


async def test_no_streak_means_no_call():
    editor = Editor([])
    events, done = await _run(editor, draft="*She waits by the fire.*")
    assert editor.calls == []
    assert not any(ev.get("step") == "subject_fixation" for ev in events)
    assert done["draft"] is None


async def test_without_history_tags_the_draft_is_never_tagged(tagger):
    editor = Editor([])
    await _run(editor, history=None)
    assert tagger == [] and editor.calls == []
