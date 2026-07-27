"""Storage and route coverage for the workflow on/off toggles.

Pins the single-source-of-truth contracts: the global flag round-trips through
``update_settings``; per-workflow flips go through the per-key JSON1 writer so two
keys coexist without clobber; the dedicated toggle route returns the decoded map;
and the manifest keeps listing a disabled workflow with no ``enabled`` field (the
frontend recomputes effectiveness from settings, so a server-resolved field would
just go stale).
"""

from __future__ import annotations

from backend.database import get_settings, set_workflow_enabled, update_settings


async def test_per_key_write_keeps_both_keys(client):
    await set_workflow_enabled("tts", False)
    await set_workflow_enabled("format_consistency", False)
    s = await get_settings()
    assert s.get("workflow_enabled") == {"tts": False, "format_consistency": False}


async def test_per_key_flip_back_to_true(client):
    await set_workflow_enabled("tts", False)
    await set_workflow_enabled("tts", True)
    s = await get_settings()
    assert s.get("workflow_enabled", {})["tts"] is True


async def test_global_flag_round_trip(client):
    await update_settings({"workflows_globally_enabled": False})
    assert (await get_settings())["workflows_globally_enabled"] == 0
    await update_settings({"workflows_globally_enabled": True})
    assert (await get_settings())["workflows_globally_enabled"] == 1


async def test_toggle_route_returns_decoded_map_without_clobber(client):
    resp = await client.post("/api/workflows/tts/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["workflow_enabled"] == {"tts": False}

    resp2 = await client.post("/api/workflows/format_consistency/enabled", json={"enabled": False})
    assert resp2.status_code == 200
    assert resp2.json()["workflow_enabled"] == {"tts": False, "format_consistency": False}


async def test_toggle_route_unregistered_404(client):
    resp = await client.post("/api/workflows/not_a_workflow/enabled", json={"enabled": False})
    assert resp.status_code == 404


async def test_toggle_route_missing_body_is_422(client):
    # enabled is required (no default), mirroring the config route: a body without
    # it is a 422, never an implicit value.
    resp = await client.post("/api/workflows/tts/enabled", json={})
    assert resp.status_code == 422


async def test_manifest_lists_disabled_workflow_with_no_enabled_field(client):
    await set_workflow_enabled("tts", False)
    body = (await client.get("/api/workflows")).json()
    tts = next((w for w in body if w["id"] == "tts"), None)
    assert tts is not None, "a disabled workflow must stay in the manifest"
    # Enablement is settings state, never a manifest field: the manifest
    # describes what exists and how it loads, and the toggle route owns
    # whether it runs. `load_status` is the *other* axis (can the active
    # revision run at all) and is always "available" for a built-in.
    assert set(tts.keys()) == {
        "id",
        "display_name",
        "config_schema",
        "config_defaults",
        "source",
        "frontend_kind",
        "load_status",
        "diagnostic",
    }
    assert tts["load_status"] == "available"
