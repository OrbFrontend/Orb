"""Regressions for imported data, model ownership, and cancelled persistence."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import uuid

import pytest

from backend import database
from backend.api.routes import characters, presets
from backend.core import maintenance_lock
from backend.features.cards.parsing import to_png
from backend.pipeline import persistence
from backend.pipeline.state import TurnState


@pytest.mark.parametrize("card_id", ['x"><img src=x onerror=alert(1)>', "a/b", "", "x" * 129])
async def test_character_creation_rejects_unsafe_ids(client, card_id):
    await client.post_checked("/api/characters", json={"id": card_id, "name": "Imported"}, expected_status=422)


async def test_png_import_ignores_unsafe_embedded_id(client):
    png = to_png({"id": 'x"><img src=x onerror=alert(1)>', "name": "Imported"})
    imported = await client.post_json("/api/characters/import", files={"file": ("card.png", png, "image/png")})
    assert imported["id"] == str(uuid.UUID(bytes=hashlib.sha256(png).digest()[:16], version=5))
    assert (await client.post_json("/api/characters", json=imported))["id"] == imported["id"]


async def test_png_import_rejects_oversized_upload_before_parsing(client, monkeypatch):
    monkeypatch.setattr(characters, "_MAX_CARD_UPLOAD", 64)
    await client.post_checked(
        "/api/characters/import", files={"file": ("card.png", b"x" * 65, "image/png")}, expected_status=413
    )


@pytest.mark.parametrize("field,role", [("active_model_config_id", "writer"), ("agent_active_model_config_id", "agent")])
@pytest.mark.parametrize("invalid_selection", ["foreign", "wrong_role", "missing"])
async def test_endpoint_selection_requires_owned_model_in_correct_lane(client, field, role, invalid_selection):
    endpoint = await client.post_json("/api/endpoints", json={"url": "https://selected.test", "api_key": "original"})
    foreign = await client.post_json("/api/endpoints", json={"url": "https://foreign.test"})
    configs = await client.get_json(
        f"/api/endpoints/{foreign['id'] if invalid_selection == 'foreign' else endpoint['id']}/models"
    )
    selected_role = ("agent" if role == "writer" else "writer") if invalid_selection == "wrong_role" else role
    config_id = next(config["id"] for config in configs if config["role"] == selected_role)
    if invalid_selection == "missing":
        config_id += 100000
    await client.put_checked(
        f"/api/endpoints/{endpoint['id']}", json={field: config_id, "api_key": "changed"}, expected_status=422
    )
    assert (await client.get_json(f"/api/endpoints/{endpoint['id']}"))["api_key"] == "original"


async def test_settings_do_not_follow_legacy_foreign_model_references(client, db):
    selected = await client.post_json("/api/endpoints", json={"url": "https://selected.test", "api_key": "selected-key"})
    foreign = await client.post_json("/api/endpoints", json={"url": "https://foreign.test", "api_key": "foreign-key"})
    await db.execute(
        "UPDATE endpoints SET active_model_config_id = ?, agent_active_model_config_id = ? WHERE id = ?",
        (foreign["active_model_config_id"], foreign["agent_active_model_config_id"], selected["id"]),
    )
    await db.commit()
    await client.put_checked(
        "/api/settings",
        json={"active_endpoint_id": selected["id"], "agent_endpoint_id": selected["id"], "agent_same_as_writer": False},
    )
    settings = await client.get_json("/api/settings")
    assert settings["endpoint_url"] == "https://selected.test"
    assert settings["api_key"] == "selected-key"
    assert settings.get("agent_endpoint_url") != "https://foreign.test"
    assert settings.get("agent_api_key") != "foreign-key"


@pytest.mark.parametrize(
    "kind,field",
    [
        ("settings", "user_name"),
        ("endpoint", "url"),
        ("endpoint", "proxy"),
        ("model", "extra_headers"),
        ("model", "model_name"),
    ],
)
async def test_nonnullable_updates_return_validation_errors(client, kind, field):
    endpoint = await client.post_json("/api/endpoints", json={"url": "https://selected.test"})
    path = {
        "settings": "/api/settings",
        "endpoint": f"/api/endpoints/{endpoint['id']}",
        "model": f"/api/models/{endpoint['active_model_config_id']}",
    }[kind]
    await client.put_checked(path, json={field: None}, expected_status=422)


async def test_nullable_references_and_samplers_can_still_be_cleared(client):
    endpoint = await client.post_json("/api/endpoints", json={"url": "https://selected.test"})
    samplers = dict.fromkeys(("temperature", "min_p", "top_k", "top_p", "repetition_penalty", "max_tokens"))
    model = await client.put_json(f"/api/models/{endpoint['active_model_config_id']}", json=samplers)
    assert all(model[field] is None for field in samplers)
    await client.put_checked(
        f"/api/endpoints/{endpoint['id']}", json={"active_model_config_id": None, "agent_active_model_config_id": None}
    )
    await client.put_checked(
        "/api/settings", json={"active_persona_id": None, "active_endpoint_id": None, "agent_endpoint_id": None}
    )


async def test_failed_reply_save_rolls_back_director_state_and_message(client, db):
    await database.create_conversation("atomic", "Atomic", "Bot", "")
    await database.update_director_state("atomic", ["calm"], keywords=["kept"], macro_choices={"choice": "old"})
    await db.execute(
        "CREATE TRIGGER fail_leaf BEFORE UPDATE OF active_leaf_id ON conversations BEGIN SELECT RAISE(ABORT, 'save failed'); END"
    )
    await db.commit()
    res = TurnState(resp_text="A reply.", active_moods=["tense"], macro_choices={"choice": "new"})
    with pytest.raises(sqlite3.IntegrityError, match="save failed"):
        await persistence._persist_result("atomic", res, {"enable_agent": True}, None, 1)
    assert await database.get_director_state("atomic") == {
        "conversation_id": "atomic",
        "active_moods": ["calm"],
        "keywords": ["kept"],
        "macro_choices": {"choice": "old"},
    }
    assert await db.all("SELECT id FROM messages WHERE conversation_id = 'atomic'") == []
    await db.execute("DROP TRIGGER fail_leaf")
    await db.commit()
    message_id, _, _ = await persistence._persist_result("atomic", res, {"enable_agent": True}, None, 1)
    assert message_id is not None
    state = await database.get_director_state("atomic")
    assert state["active_moods"] == ["tense"] and state["macro_choices"] == {"choice": "new"}
    assert state["keywords"] == ["kept"]


@pytest.mark.parametrize("operation", ["apply", "restore"])
async def test_cancelled_preset_worker_keeps_both_maintenance_guards(client, monkeypatch, operation):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(*_args):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("test worker was not released")
        return {}

    monkeypatch.setattr(presets.presets, "_library_path", lambda _name: "test.db")
    monkeypatch.setattr(presets.presets, "create_snapshot", lambda _label: "before.db")
    monkeypatch.setattr(presets.presets, "read_meta", lambda _path: {})
    monkeypatch.setattr(presets.presets, "apply_preset" if operation == "apply" else "restore_full", blocked)
    route = presets.api_apply_preset if operation == "apply" else presets.api_restore_preset
    task = asyncio.create_task(route("test.db"))

    async def competing_lock():
        async with maintenance_lock():
            return True

    contender = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        contender = asyncio.create_task(competing_lock())
        await asyncio.sleep(0)
        assert not contender.done()
        await client.post_checked("/api/characters", json={"name": "Blocked"}, expected_status=409)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if contender is not None:
            assert await contender
    await client.post_checked("/api/characters", json={"name": "Allowed"})
