"""Integration tests for the TTS workflow's pipeline and trigger hooks.

The `tts` workflow registers at import, so these tests do not clear the
registry -- they exercise the live hooks against a temp DB. A stub adapter
stands in for a real TTS backend.
"""

from __future__ import annotations

import base64
import json
from types import MappingProxyType
from unittest.mock import patch

import pytest

from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    get_messages,
    get_workflow_attachment_by_id,
)
from backend.inference import LLMClient, _KVCacheTracker
from backend.pipeline.orchestrator import _run_pipeline
from backend.workflows import (
    HookType,
    PostCtx,
    RegenCtx,
    get_workflow,
    set_workflow_character_state,
    set_workflow_config,
    workflow_has_hook,
)
from backend.workflows.tts import hooks
from backend.workflows.tts.engine.base import SynthesisResult, TTSAdapter


class _FakeAdapter(TTSAdapter):
    @property
    def backend_name(self) -> str:
        return "Fake"

    async def synthesize(self, chunks, voice_id, language="en-US", rate=1.0, pitch=1.0, **kwargs):
        return SynthesisResult(audio_bytes=b"FAKEAUDIO", content_type="audio/mpeg")

    async def list_voices(self, language="", **kwargs):
        return [{"id": "v1", "name": "Voice One"}]


@pytest.fixture
def fake_adapter(monkeypatch):
    monkeypatch.setattr("backend.workflows.tts.synth.get_adapter", lambda backend: _FakeAdapter())


def _post_ctx(cid: str, char_id: str, draft: str) -> PostCtx:
    return PostCtx(
        conversation_id=cid,
        history=(),
        draft=draft,
        effective_msg="",
        director_output=MappingProxyType({}),
        settings=MappingProxyType({}),
        prefix=(),
        enabled_tools=MappingProxyType({}),
        turn_scratch={},
        client=None,
        kv_tracker=None,
        schema_overrides=MappingProxyType({}),
        character_id=char_id,
    )


async def _seed(cid: str = "conv1", char_id: str = "char1") -> tuple[str, str]:
    await create_character_card({"id": char_id, "name": "Tester"})
    await create_conversation(cid, "Title", "Tester", "scene", character_card_id=char_id)
    return cid, char_id


async def test_post_pipeline_attaches_audio_when_enabled(client, fake_adapter):
    cid, char_id = await _seed()
    await set_workflow_config("tts", {"auto_play": False, "volume": 0.75})
    await set_workflow_character_state(char_id, "tts", {"enabled": True, "backend": "edge", "voice_id": "v1"})

    events = [ev async for ev in hooks.post_pipeline(_post_ctx(cid, char_id, '"Hello there."'))]

    attach = [e for e in events if e.get("type") == "attach_artifact"]
    assert len(attach) == 1
    att = attach[0]["attachment"]
    assert att["workflow_id"] == "tts"
    assert att["source"] == "workflow:tts"
    assert att["mime"] == "audio/mpeg"
    assert att["data"] == b"FAKEAUDIO"
    assert isinstance(att["seed"], str) and att["seed"]
    assert att["generation_metadata"]["text"] == '"Hello there."'
    assert att["consumption_metadata"]["blocks"]
    assert not any(e.get("event") == "tts_autoplay" for e in events)


async def test_post_pipeline_emits_autoplay_signal(client, fake_adapter):
    cid, char_id = await _seed()
    await set_workflow_config("tts", {"auto_play": True, "volume": 0.75})
    await set_workflow_character_state(char_id, "tts", {"enabled": True, "backend": "edge", "voice_id": "v1"})

    events = [ev async for ev in hooks.post_pipeline(_post_ctx(cid, char_id, '"Hi."'))]

    assert any(e.get("event") == "tts_autoplay" for e in events)


async def test_post_pipeline_skips_when_profile_disabled(client, fake_adapter):
    cid, char_id = await _seed()
    await set_workflow_config("tts", {"auto_play": False, "volume": 0.75})
    await set_workflow_character_state(char_id, "tts", {"enabled": False, "backend": "edge", "voice_id": "v1"})

    events = [ev async for ev in hooks.post_pipeline(_post_ctx(cid, char_id, "Hello."))]

    assert events == []


async def test_run_pipeline_autogenerates_attachment_end_to_end(client, fake_adapter):
    # Drives the REAL tts post_pipeline hook through the orchestrator pipeline
    # (the path a normal turn takes), rather than calling the hook directly.
    cid, char_id = await _seed()
    await set_workflow_config("tts", {"auto_play": False, "volume": 0.75})
    await set_workflow_character_state(char_id, "tts", {"enabled": True, "backend": "edge", "voice_id": "v1"})

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": '"Hello there."'}

    with patch("backend.pipeline.passes.writer.writer_pass", new=mock_writer):
        events = [
            ev
            async for ev in _run_pipeline(
                LLMClient("http://localhost:9999"),
                {"model_name": "test", "enable_agent": 1, "enabled_tools": {}, "reasoning_enabled_passes": {}},
                {"active_moods": []},
                [],
                [],
                "hi",
                conversation_id=cid,
                character_id=char_id,
                prefix=[{"role": "system", "content": "You are an assistant."}],
                enabled_tools={},
                turn_scratch={},
                kv_tracker=_KVCacheTracker(),
                schema_overrides={},
            )
        ]

    [result] = [e for e in events if e["event"] == "_result"]
    staged = result["data"]["staged_attachments"]
    assert len(staged) == 1
    assert staged[0]["workflow_id"] == "tts"
    assert staged[0]["source"] == "workflow:tts"
    assert staged[0]["data"] == b"FAKEAUDIO"


async def test_full_send_turn_persists_audio_attachment(client, llm_mock, fake_adapter):
    # The real /send path: HTTP -> handle_turn -> _run_pipeline -> POST_PIPELINE
    # -> _persist_result -> add_message. Asserts the audio attachment lands on
    # the persisted assistant message.
    cid, char_id = await _seed()
    await set_workflow_config("tts", {"auto_play": False, "volume": 0.75})
    await set_workflow_character_state(char_id, "tts", {"enabled": True, "backend": "edge", "voice_id": "v1"})
    llm_mock.enqueue_writer('"A spoken reply."')
    llm_mock.enqueue_editor(None)

    resp = await client.post(f"/api/conversations/{cid}/send", json={"content": "hi", "attachments": []})
    assert resp.status_code == 200
    _ = resp.text  # drain the buffered SSE stream so the turn completes

    msgs = await get_messages(cid)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert assistant, "no assistant message persisted"
    atts = [a for a in (assistant[-1].get("workflow_attachments") or []) if a.get("workflow_id") == "tts"]
    assert len(atts) == 1, f"expected one tts attachment on the reply, got {len(atts)}"


async def test_create_trigger_inserts_attachment(client, fake_adapter):
    cid, char_id = await _seed()
    mid, _ = await add_message(cid, "assistant", '"Spoken line."', 0)

    resp = await client.post(
        f"/api/conversations/{cid}/workflows/tts/trigger",
        json={"action": "create", "message_id": mid},
    )

    assert resp.status_code == 200
    new_id = resp.json().get("attachment_id")
    assert isinstance(new_id, int)
    row = await get_workflow_attachment_by_id(new_id)
    assert row is not None
    assert row["workflow_id"] == "tts"
    assert row["message_id"] == mid


async def test_create_trigger_rejects_non_assistant_message(client, fake_adapter):
    cid, char_id = await _seed()
    mid, _ = await add_message(cid, "user", "a question", 0)

    resp = await client.post(
        f"/api/conversations/{cid}/workflows/tts/trigger",
        json={"action": "create", "message_id": mid},
    )

    assert resp.status_code == 200
    assert "error" in resp.json()


async def test_regenerate_uses_current_profile_not_stored_metadata(client, fake_adapter):
    cid, char_id = await _seed()
    mid, _ = await add_message(cid, "assistant", '"Spoken line."', 0)
    # Live profile differs from whatever the attachment was first generated with.
    await set_workflow_character_state(
        char_id, "tts", {"enabled": True, "backend": "edge", "voice_id": "v-new", "rate": 1.7, "pitch": 0.6}
    )
    stale = MappingProxyType(
        {
            "generation_metadata": json.dumps(
                {"backend": "edge", "voice_id": "v-old", "rate": 1.0, "pitch": 1.0, "text": "Spoken line."}
            )
        }
    )
    ctx = RegenCtx(
        conversation_id=cid,
        message_id=mid,
        attachment_id=1,
        original_attachment=stale,
        history=(),
        last_user_message="",
        settings=MappingProxyType({}),
        client=None,
        character_id=char_id,
    )

    out = await hooks.regenerate(ctx, {})

    assert len(out) == 1
    md = out[0]["generation_metadata"]
    assert md["voice_id"] == "v-new"
    assert md["rate"] == 1.7
    assert md["pitch"] == 0.6
    assert md["text"] == '"Spoken line."'


async def test_rehydrate_regenerates_consumption_metadata(client, fake_adapter):
    """Rehydrate re-synthesizes the audio, and TTS output is not guaranteed
    byte-identical across runs, so the restored row's byte ranges and word
    timings must be rebuilt to match the new bytes -- not left describing the
    evicted ones. Pins that the rehydrate route persists the metadata the
    reroll_gen hook returns, overwriting the stale stored value."""
    from backend.database import insert_workflow_attachment_row, set_active_leaf
    from backend.database.connection import get_db
    from backend.workflows.attachment_cache import evict

    cid, char_id = await _seed()
    mid, _ = await add_message(cid, "assistant", '"Hello there."', 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "speech.mp3",
            "mime": "audio/mpeg",
            "data": b"ORIGINAL_AUDIO_BYTES",
            "workflow_id": "tts",
            "seed": "TTS-SEED",
            "generation_metadata": {"backend": "edge", "voice_id": "v1", "text": '"Hello there."'},
        },
    )
    # Stale metadata deliberately inconsistent with the bytes rehydrate will produce: byte_end 999 and an empty word list, so the assertions below prove rehydrate overwrote it.
    async with get_db() as conn:
        await conn.execute(
            "UPDATE workflow_attachments SET consumption_metadata = ? WHERE id = ?",
            (json.dumps({"blocks": [{"byte_start": 0, "byte_end": 999, "pause_after_ms": 0, "words": []}]}), aid),
        )
        await conn.commit()
    await evict(aid)

    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
        json={},
    )
    assert resp.status_code == 200

    row = await get_workflow_attachment_by_id(aid)
    assert row is not None
    assert row["data_b64"] == base64.b64encode(b"FAKEAUDIO").decode("ascii")
    cm = json.loads(row["consumption_metadata"])
    assert len(cm["blocks"]) == 1
    blk = cm["blocks"][0]
    # Rebuilt against the restored bytes (len("FAKEAUDIO") == 9), not the stale 999.
    assert blk["byte_start"] == 0
    assert blk["byte_end"] == len(b"FAKEAUDIO")
    # Word timings regenerated: the estimator yields one span per alignable token
    # of the dialogue ("Hello", "there.").
    assert len(blk["words"]) == 2


async def test_config_round_trip(client):
    payload = {"config": {"auto_play": True, "volume": 0.4}}
    put = await client.put("/api/workflows/tts/config", json=payload)
    assert put.status_code == 200
    assert put.json() == payload
    got = await client.get("/api/workflows/tts/config")
    assert got.json() == payload


async def test_config_defaults_on_fresh_slot(client):
    got = await client.get("/api/workflows/tts/config")
    assert got.json() == {
        "config": {
            "auto_play": False,
            "volume": 0.75,
            "click_granularity": "block",
            "click_play_scope": "unit",
            "show_karaoke": True,
        }
    }


async def test_profile_get_set_round_trip(client):
    cid, char_id = await _seed()
    base = f"/api/conversations/{cid}/workflows/tts/trigger"

    got = await client.post(base, json={"action": "get_profile"})
    assert got.status_code == 200
    assert got.json()["profile"]["enabled"] is False

    saved = await client.post(
        base,
        json={"action": "set_profile", "profile": {"enabled": True, "backend": "edge", "voice_id": "v9", "rate": 1.2}},
    )
    assert saved.status_code == 200
    assert saved.json()["ok"] is True

    again = await client.post(base, json={"action": "get_profile"})
    profile = again.json()["profile"]
    assert profile["enabled"] is True
    assert profile["voice_id"] == "v9"
    assert profile["rate"] == 1.2


def test_tts_binds_both_dispatch_hooks():
    # QUERY (conversation-less discovery) and ON_DEMAND (conversation-scoped
    # create / profile) are distinct single-dispatch slots; TTS binds both.
    workflow = get_workflow("tts")
    assert workflow is not None
    assert workflow_has_hook(workflow, HookType.QUERY)
    assert workflow_has_hook(workflow, HookType.ON_DEMAND)


async def test_query_route_lists_backends_without_a_conversation(client):
    # list_backends reads the static backend registry: no conversation, no
    # character, no backend probe -- exactly what QUERY exists to answer, and
    # why it moved off the per-conversation trigger.
    resp = await client.post("/api/workflows/tts/query", json={"action": "list_backends"})
    assert resp.status_code == 200
    backends = resp.json()["backends"]
    assert isinstance(backends, list) and backends
    assert all("id" in b for b in backends)


async def test_query_route_previews_from_the_request_profile(client, fake_adapter):
    # Preview synthesizes the unsaved profile carried in the body -- no message,
    # no character -- and returns the audio inline for the config panel to play.
    resp = await client.post(
        "/api/workflows/tts/query",
        json={"action": "preview", "backend": "edge", "voice_id": "v1", "text": "Hi."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert base64.b64decode(body["audio_b64"]) == b"FAKEAUDIO"
    assert body["mime"] == "audio/mpeg"


async def test_query_route_rejects_unknown_action_in_band(client):
    resp = await client.post("/api/workflows/tts/query", json={"action": "does_not_exist"})
    assert resp.status_code == 200
    assert "unknown action" in resp.json()["error"]


async def test_discovery_actions_left_the_conversation_trigger(client):
    # The migrated actions are no longer served by on_demand: the trigger router
    # reports them as unknown, which is the proof they moved to QUERY rather than
    # being answered by both surfaces.
    cid, _ = await _seed()
    base = f"/api/conversations/{cid}/workflows/tts/trigger"
    for action in ("list_backends", "list_voices", "list_models", "preview"):
        resp = await client.post(base, json={"action": action})
        assert resp.status_code == 200
        assert "unknown action" in resp.json()["error"], action
