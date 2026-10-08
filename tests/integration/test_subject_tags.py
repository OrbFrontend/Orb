"""Subject tags stored with replies: tagged at save, reused while current, re-tagged when the text or version changes."""

from typing import cast

import pytest

import backend.database as dbmod
import backend.database.connection as db_connection
from backend.core.settings import Settings
from backend.inference import local_ml
from backend.pipeline import subject_tags

ON = cast(
    Settings,
    {
        "enable_agent": 1,
        "decision_endpoint_id": 1,
        "decision_model": "typesafe/jev-1.13",
        "enabled_tools": {"editor_apply_patch": True},
        "editor_audit_toggles": {"subject_fixation": True},
        "local_ml_enabled": {},
    },
)


@pytest.fixture
async def tagger(db_path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    monkeypatch.setattr(local_ml, "available", lambda feature: (True, ""))
    monkeypatch.setattr(subject_tags, "_memo", {})
    seen: list[str] = []

    async def fake(narration: str):
        seen.append(narration)
        return {"hair": [0.0, 0.0, 1.0] if "copper" in narration else [1.0, 0.0, 0.0]}

    monkeypatch.setattr(local_ml, "aclassify_subjects", fake)
    return seen


async def _branch(texts: list[str]) -> list[dict]:
    await dbmod.create_conversation("c", "t", "Lyra", "")
    parent, rows = None, []
    for i, text in enumerate(texts):
        role = "assistant" if i % 2 else "user"
        mid, _ = await dbmod.add_message("c", role, text, i, parent_id=parent)
        rows.append({"id": mid, "role": role, "content": text})
        parent = mid
    return rows


async def test_tags_are_stored_at_save_and_reused(tagger):
    rows = await _branch(["hi", '*Her copper braid gleams.* "Hello."', "ok", "*She waits.*"])
    for row in rows[1::2]:
        await subject_tags.tag_saved_reply(row["id"], row["content"], ON)
    assert tagger == ["*Her copper braid gleams.*", "*She waits.*"]  # narration only

    tagger.clear()
    subject_tags._memo.clear()
    tags = await subject_tags.branch_tags(rows, window=6)
    assert tagger == []  # read back, not recomputed
    assert [t.probs["hair"][2] for t in tags] == [0.0, 1.0]  # newest first


async def test_an_edited_or_untagged_reply_is_tagged_on_read(tagger):
    rows = await _branch(["hi", "*Her copper braid gleams.*", "ok", "*She waits.*"])
    await subject_tags.tag_saved_reply(rows[1]["id"], rows[1]["content"], ON)  # rows[3] never tagged
    await dbmod.update_message_content(rows[1]["id"], "*She turns away.*")
    rows[1]["content"] = "*She turns away.*"
    tagger.clear()

    tags = await subject_tags.branch_tags(rows, window=6)
    assert sorted(tagger) == ["*She turns away.*", "*She waits.*"]
    assert [t.probs["hair"][2] for t in tags] == [0.0, 0.0]
    stored = await dbmod.get_message_subjects([rows[1]["id"], rows[3]["id"]])
    assert {r["content_hash"] for r in stored.values()} == {
        subject_tags.content_hash("*She turns away.*"),
        subject_tags.content_hash("*She waits.*"),
    }


async def test_a_new_model_version_re_tags(tagger, monkeypatch):
    rows = await _branch(["hi", "*Her copper braid gleams.*"])
    await subject_tags.tag_saved_reply(rows[1]["id"], rows[1]["content"], ON)
    monkeypatch.setattr(subject_tags, "tag_version", lambda: "other-model|subjects-input-v1")
    subject_tags._memo.clear()
    tagger.clear()
    await subject_tags.branch_tags(rows, window=6)
    assert tagger == ["*Her copper braid gleams.*"]


async def test_tags_go_with_their_message(tagger):
    rows = await _branch(["hi", "*Her copper braid gleams.*"])
    await subject_tags.tag_saved_reply(rows[1]["id"], rows[1]["content"], ON)
    await dbmod.delete_message_with_descendants("c", rows[1]["id"])
    assert await dbmod.get_message_subjects([rows[1]["id"]]) == {}


async def test_a_group_member_is_counted_on_their_own_replies(tagger):
    await dbmod.create_conversation("g", "t", "Scene", "")
    parent, rows = None, []
    for i, (speaker, text) in enumerate(
        [("ana", "*Her copper braid gleams.*"), ("bo", "*He shrugs.*"), ("ana", "*Her copper braid again.*")]
    ):
        mid, _ = await dbmod.add_message("g", "assistant", text, i, parent_id=parent)  # the speaker rides the history row
        rows.append({"id": mid, "role": "assistant", "content": text, "speaker_member_id": speaker})
        parent = mid
    ana = await subject_tags.branch_tags(rows, window=6, speaker_member_id="ana")
    assert [t.probs["hair"][2] for t in ana] == [1.0, 1.0]
    assert len(await subject_tags.branch_tags(rows, window=6)) == 3  # solo chats pass no speaker: every reply counts
