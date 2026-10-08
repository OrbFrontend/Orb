"""A whole turn with the subject fixation edit on: one forced exact edit, the reply saved edited and tagged."""

from typing import Literal

import pytest

import backend.database as dbmod
from backend.analysis.audit import AUDIT_TYPES
from backend.inference import DecisionClient, local_ml
from backend.pipeline import handle_turn, subject_tags


async def _fake_tagger(narration: str):
    return {"hair": [0.0, 0.0, 1.0] if "copper" in narration else [1.0, 0.0, 0.0]}


@pytest.fixture(autouse=True)
def _tagger(monkeypatch):
    monkeypatch.setattr(local_ml, "available", lambda feature: (True, ""))
    monkeypatch.setattr(local_ml, "aclassify_subjects", _fake_tagger)
    monkeypatch.setattr(subject_tags, "_memo", {})


@pytest.fixture(autouse=True)
async def judge_reads(client, monkeypatch) -> list[str]:
    """A configured Judge; hair in every recent reply is a presence streak, so it is never asked."""
    reads: list[str] = []

    async def decide(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
        reads.append(state)
        raise AssertionError("a presence streak needs no Judge")

    monkeypatch.setattr(DecisionClient, "decide", decide)
    endpoint = await client.post_json("/api/endpoints", json={"url": "https://judge.test/api/v1", "kind": "judge"})
    await client.put_json(
        "/api/decisions/config", json={"decision_endpoint_id": endpoint["id"], "decision_model": "typesafe/jev-1.13"}
    )
    return reads


def _enqueue_fix(llm_mock) -> None:
    llm_mock.enqueue_post_processing(
        [
            {
                "id": "f1",
                "type": "function",
                "function": {
                    "name": "editor_find_replace",
                    "arguments": {"patches": [{"find": "Her copper braid gleams in the dark.", "replace": "She looks up."}]},
                },
            }
        ]
    )


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


async def test_a_streaking_reply_is_edited_saved_and_tagged(client, llm_mock, judge_reads):
    cid = "conv-fixation"
    await dbmod.create_conversation(cid, "fixation", "Lyra", "a scenario")
    parent = None
    for turn in range(6):
        parent, _ = await dbmod.add_message(cid, "user", f"line {turn}", 2 * turn, parent_id=parent)
        parent, _ = await dbmod.add_message(
            cid, "assistant", f"*Her copper braid gleams, {turn}.*", 2 * turn + 1, parent_id=parent
        )
    await dbmod.set_active_leaf(cid, parent)
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": False, "editor_apply_patch": True},
            "editor_audit_toggles": {**dict.fromkeys(AUDIT_TYPES, False), "subject_fixation": True},
        },
    )

    llm_mock.enqueue_writer('*Her copper braid gleams in the dark.* "You came back."')
    _enqueue_fix(llm_mock)

    await _drain(handle_turn(cid, "hello"))

    assert [name for name, _ in llm_mock.calls if name == "post_processing"] == ["post_processing"]
    saved = (await dbmod.get_active_path(cid))[-1]
    assert saved["content"] == '*She looks up.* "You came back."'
    [row] = (await dbmod.get_message_subjects([saved["id"]])).values()
    assert row["probs"]["hair"][2] == 0.0 and judge_reads == []


async def test_a_group_steer_reads_the_speakers_replies_past_the_audit_window(client, llm_mock):
    """With six other replies between each of Aria's, her last four reach past the audit's twenty newest replies."""
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
            "editor_audit_toggles": {**dict.fromkeys(AUDIT_TYPES, False), "subject_fixation": True},
        },
    )
    cards = [await client.create("/api/characters", json={"name": name}) for name in ("Aria", "Kael")]
    conv = await client.post_json(
        "/api/conversations", json={"kind": "group", "members": [{"character_card_id": card} for card in cards]}
    )
    aria, kael = (m["id"] for m in await client.get_json(f"/api/conversations/{conv['id']}/members"))
    rows: list[tuple[Literal["user", "assistant"], str, str | None, str]] = [("user", "Begin.", None, "e0")]
    for k in range(4):
        rows.append(("assistant", f"*Her copper braid gleams, {k}.*", aria, f"e{k}"))
        rows.extend(("assistant", f"*Kael shrugs, {k}.{j}.*", kael, f"e{k}") for j in range(6))
    rows += [("user", "And then?", None, "t"), ("assistant", "*Aria waits.*", aria, "t")]
    parent = None
    for index, (role, content, speaker, exchange) in enumerate(rows):
        parent, _ = await dbmod.add_message(
            conv["id"], role, content, index, parent_id=parent, speaker_member_id=speaker, exchange_id=exchange
        )
    await dbmod.set_active_leaf(conv["id"], parent)

    llm_mock.enqueue_director(
        [{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": [], "speaking_plan": ["aria — Go"]}}}]
    )
    llm_mock.enqueue_writer("*Her copper braid gleams in the dark.*")
    _enqueue_fix(llm_mock)
    await client.post_checked(f"/api/conversations/{conv['id']}/messages/{parent}/super_regenerate", json={})

    assert [name for name, _ in llm_mock.calls if name == "post_processing"] == ["post_processing"]
