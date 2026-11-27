"""Profiles: leaving a profile saves the live state into it, entering one makes it live, each keeps its own library, Backups
work by profile and part, and migration 0101 turns the prompt layouts into profiles with their own copies."""

import importlib
import json
import sqlite3
from contextlib import closing

from backend.api.work_registry import work_registry
from backend.database import get_active_layout_blocks, get_profile
from backend.database.schema import CREATE_TABLES_SQL

_MIGRATION = importlib.import_module("backend.database.migrations.0101_profiles")
_LIBRARY = (
    "mood_fragments",
    "interactive_fragments",
    "phrase_bank",
    "slop_dismissals",
    "slop_suggestions",
    "slop_mining_state",
)


async def _live(client) -> dict:
    """The parts of the live state a profile carries, as the test changes them."""
    settings = await client.get_json("/api/settings")
    endpoint = await client.get_json(f"/api/endpoints/{settings['active_endpoint_id']}")
    fragments = {f["id"]: f["enabled"] for f in await client.get_json("/api/fragments")}
    return {
        "max_words": settings["length_guard_max_words"],
        "direct_scene": settings["enabled_tools"].get("direct_scene"),
        "writer_endpoint": settings["active_endpoint_id"],
        "writer_config": endpoint["active_model_config_id"],
        "model_name": settings["model_name"],
        "fragments": fragments,
    }


async def test_switching_round_trips_each_profile_and_skips_a_deleted_lane(client):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    second = await client.post_json("/api/endpoints", json={"url": "http://second/v1"})
    b_id = (await client.post_json("/api/profiles", json={"name": "B"}))["id"]
    baseline = await _live(client)

    # Under B: the second endpoint writes.
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    await client.put_checked("/api/settings", json={"active_endpoint_id": second["id"]})
    assert (await client.put_json("/api/profiles/active", json={"id": a_id}))["active_endpoint_id"] == baseline[
        "writer_endpoint"
    ]

    # Under A: change the length guard, a tool, the writer model config and a fragment.
    fragment = next(iter(baseline["fragments"]))
    config = await client.post_json(
        f"/api/endpoints/{baseline['writer_endpoint']}/models", json={"model_name": "a-model", "role": "writer"}
    )
    await client.put_checked(f"/api/endpoints/{baseline['writer_endpoint']}", json={"active_model_config_id": config["id"]})
    await client.put_checked(
        "/api/settings",
        json={
            "length_guard_max_words": baseline["max_words"] + 77,
            "enabled_tools": {"direct_scene": not baseline["direct_scene"]},
        },
    )
    await client.put_checked(f"/api/fragments/{fragment}", json={"enabled": int(not baseline["fragments"][fragment])})
    edited = await _live(client)

    row = await client.put_json("/api/profiles/active", json={"id": b_id})
    assert row["active_profile_id"] == b_id and "api_key" not in row
    assert await _live(client) == {
        **baseline,
        "writer_endpoint": second["id"],
        "writer_config": second["active_model_config_id"],
        "model_name": "default",
    }
    assert {block.id.split(":")[0] for block in await get_active_layout_blocks() if ":" in block.id} == {b_id}

    await client.put_checked("/api/profiles/active", json={"id": a_id})
    assert await _live(client) == edited
    assert edited["model_name"] == "a-model"

    # B's writer endpoint is gone while B is inactive: B's other values still apply, and the lane keeps A's endpoint.
    await client.delete_checked(f"/api/endpoints/{second['id']}")
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    assert await _live(client) == {**baseline, "writer_config": config["id"], "model_name": "a-model"}


async def test_a_profile_without_a_judge_turns_the_judge_off(client):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    judge = await client.post_json("/api/endpoints", json={"url": "http://judge/v1", "kind": "judge"})
    await client.put_checked("/api/decisions/config", json={"decision_endpoint_id": None})
    b_id = (await client.post_json("/api/profiles", json={"name": "No Judge"}))["id"]
    await client.put_checked("/api/decisions/config", json={"decision_endpoint_id": judge["id"]})

    assert (await client.put_json("/api/profiles/active", json={"id": b_id}))["decision_endpoint_id"] is None
    assert (await client.put_json("/api/profiles/active", json={"id": a_id}))["decision_endpoint_id"] == judge["id"]


async def test_a_profile_carries_its_persona_document_assistant_and_workflow_settings(client):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    one = await client.post_json("/api/user-personas", json={"name": "One"})
    two = await client.post_json("/api/user-personas", json={"name": "Two"})

    async def set_up(persona: int, rules: str, style: str, comfy: str, voice: bool) -> None:
        await client.put_checked(
            "/api/settings",
            json={"active_persona_id": persona, "assistant_instructions": rules, "document_audit_autopatch": voice},
        )
        image = (await client.get_json("/api/workflows/image_gen/config"))["config"]
        image = {**image, "default_style": style, "external_comfy": {**image["external_comfy"], "api_url": comfy}}
        await client.put_checked("/api/workflows/image_gen/config", json={"config": image})
        await client.put_checked("/api/workflows/format_consistency/config", json={"config": {"voice_consistency": voice}})

    async def live() -> tuple:
        settings = await client.get_json("/api/settings")
        image = (await client.get_json("/api/workflows/image_gen/config"))["config"]
        voice = (await client.get_json("/api/workflows/format_consistency/config"))["config"]["voice_consistency"]
        return (
            settings["active_persona_id"],
            settings["assistant_instructions"],
            bool(settings["document_audit_autopatch"]),
            image["default_style"],
            image["external_comfy"]["api_url"],
            voice,
        )

    await set_up(one["id"], "A rules", "anime", "http://a:8188", True)
    b_id = (await client.post_json("/api/profiles", json={"name": "B"}))["id"]
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    await set_up(two["id"], "B rules", "realistic", "http://b:8188", False)

    # The ComfyUI address is this machine's, so it stays as B left it.
    await client.put_checked("/api/profiles/active", json={"id": a_id})
    assert await live() == (one["id"], "A rules", True, "anime", "http://b:8188", True)
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    assert await live() == (two["id"], "B rules", False, "realistic", "http://b:8188", False)

    # A's persona is deleted and another takes its name: A finds it by name.
    await client.delete_checked(f"/api/user-personas/{one['id']}")
    again = await client.post_json("/api/user-personas", json={"name": "One"})
    await client.put_checked("/api/profiles/active", json={"id": a_id})
    assert (await live())[0] == again["id"]


async def test_a_backups_apply_renumbers_profile_lanes_with_the_endpoints(client):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    await client.post_json("/api/endpoints", json={"url": "http://second/v1"})
    second = next(e for e in await client.get_json("/api/endpoints") if e["url"] == "http://second/v1")
    b_id = (await client.post_json("/api/profiles", json={"name": "B"}))["id"]
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    await client.put_checked("/api/settings", json={"active_endpoint_id": second["id"]})
    await client.put_checked("/api/profiles/active", json={"id": a_id})
    name = (await client.post_json("/api/backups/export", json={"domains": ["configs"], "strip_keys": False}))["name"]
    # Made after the snapshot, so the apply keeps it while replacing the endpoints it names.
    c_id = (await client.post_json("/api/profiles", json={"name": "C"}))["id"]

    await client.post_checked(f"/api/backups/{name}/apply", json={})

    renumbered = next(e for e in await client.get_json("/api/endpoints") if e["url"] == "http://second/v1")
    assert renumbered["id"] != second["id"]
    assert (await client.put_json("/api/profiles/active", json={"id": b_id}))["active_endpoint_id"] == renumbered["id"]
    assert (await client.get_json(f"/api/endpoints/{renumbered['id']}"))["active_model_config_id"] == renumbered[
        "active_model_config_id"
    ]
    c = await get_profile(c_id)
    assert c is not None and c["writer_endpoint_id"] is None and c["writer_model_config_id"] is None


async def test_deleting_in_one_profile_leaves_the_others_and_deleting_a_profile_backs_it_up(client):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    world = await client.post_json("/api/worlds", json={"name": "Shared", "is_global": True})
    judge = await client.post_json("/api/endpoints", json={"url": "http://judge/v1", "kind": "judge"})
    await client.put_checked("/api/decisions/config", json={"decision_endpoint_id": judge["id"]})
    b_id = (await client.post_json("/api/profiles", json={"name": "B", "copy_from": a_id}))["id"]
    await client.put_checked("/api/decisions/config", json={"decision_endpoint_id": None})
    fragment = (await client.get_json("/api/fragments"))[0]["id"]
    phrase = (await client.get_json("/api/phrase-bank"))[0]

    await client.delete_checked(f"/api/fragments/{fragment}")
    await client.delete_checked(f"/api/phrase-bank/{phrase['id']}")
    await client.put_checked(f"/api/worlds/{world['id']}", json={"is_global": False})

    await client.put_checked("/api/profiles/active", json={"id": b_id})
    assert fragment in {f["id"] for f in await client.get_json("/api/fragments")}
    assert phrase["pattern"] in {p["pattern"] for p in await client.get_json("/api/phrase-bank")}
    assert next(w for w in await client.get_json("/api/worlds") if w["id"] == world["id"])["is_global"]

    await client.delete_checked(f"/api/profiles/{b_id}")
    backup = (await client.get_json("/api/backups"))[0]
    assert backup["kind"] == "auto" and backup["included_domains"] == []
    assert backup["profiles"] == [{"id": b_id, "name": "B", "parts": ["setup", "fragments", "phrase_bank"]}]
    parts = ["setup", "fragments", "phrase_bank"]
    await client.post_checked(
        f"/api/backups/{backup['name']}/apply", json={"profiles": {b_id: {"target": "same", "parts": parts}}}
    )
    assert {p["id"] for p in (await client.get_json("/api/profiles"))["profiles"]} == {a_id, b_id}
    # The backup holds no endpoints, so B's lane columns came back NULL; its Judge is found again by the pick's name.
    assert (await client.put_json("/api/profiles/active", json={"id": b_id}))["decision_endpoint_id"] == judge["id"]
    assert fragment in {f["id"] for f in await client.get_json("/api/fragments")}
    assert phrase["pattern"] in {p["pattern"] for p in await client.get_json("/api/phrase-bank")}


async def test_profiles_do_not_switch_while_a_generation_runs(client):
    b_id = (await client.post_json("/api/profiles", json={"name": "B"}))["id"]
    stream = await work_registry.begin_stream("some-chat", None, None)
    try:
        response = await client.put("/api/profiles/active", json={"id": b_id})
        assert response.status_code == 409
    finally:
        work_registry.finish_stream("some-chat", stream)
    assert (await client.put_json("/api/profiles/active", json={"id": b_id}))["active_profile_id"] == b_id


async def _label(client, fid: str) -> str:
    return next(f["label"] for f in await client.get_json("/api/fragments") if f["id"] == fid)


async def test_a_profile_fragments_backup_comes_back_on_its_profile_or_a_new_one(client):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    b_id = (await client.post_json("/api/profiles", json={"name": "B", "copy_from": a_id}))["id"]
    fid = (await client.get_json("/api/fragments"))[0]["id"]
    original = await _label(client, fid)
    words = (await client.get_json("/api/settings"))["length_guard_max_words"]
    name = (await client.post_json("/api/backups/export", json={"domains": [], "profiles": {a_id: ["fragments"]}}))["name"]
    [entry] = [e for e in await client.get_json("/api/backups") if e["name"] == name]
    assert entry["included_domains"] == [] and entry["profiles"] == [{"id": a_id, "name": "Default", "parts": ["fragments"]}]

    await client.put_checked(f"/api/fragments/{fid}", json={"label": "Edited in A"})
    await client.put_checked("/api/settings", json={"length_guard_max_words": words + 5})
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    await client.put_checked(f"/api/fragments/{fid}", json={"label": "Edited in B"})
    await client.put_checked("/api/profiles/active", json={"id": a_id})

    await client.post_checked(f"/api/backups/{name}/apply", json={})
    assert await _label(client, fid) == original
    assert (await client.get_json("/api/settings"))["length_guard_max_words"] == words + 5
    await client.put_checked("/api/profiles/active", json={"id": b_id})
    assert await _label(client, fid) == "Edited in B"

    # Onto a new profile: it holds the backed-up fragments and gets the built-in layout; the others keep theirs.
    await client.post_checked(
        f"/api/backups/{name}/apply", json={"profiles": {a_id: {"target": "new", "parts": ["fragments"]}}}
    )
    profiles = (await client.get_json("/api/profiles"))["profiles"]
    [new] = [p for p in profiles if p["id"] not in (a_id, b_id)]
    assert await _label(client, fid) == "Edited in B"
    await client.put_checked("/api/profiles/active", json={"id": new["id"]})
    assert await _label(client, fid) == original
    assert await client.get_json("/api/phrase-bank"), "a new profile gets the seed bank for the parts it did not take"
    assert {block.id.split(":")[0] for block in await get_active_layout_blocks() if ":" in block.id} == {new["id"]}


async def test_a_fragments_snapshot_without_configs_carries_every_profile(client, db_path):
    a_id = (await client.get_json("/api/profiles"))["active_id"]
    b_id = (await client.post_json("/api/profiles", json={"name": "B"}))["id"]
    name = (await client.post_json("/api/backups/export", json={"domains": ["fragments"]}))["name"]
    with closing(sqlite3.connect(db_path.parent / "snapshots" / name)) as conn:
        assert {r[0] for r in conn.execute("SELECT DISTINCT profile_id FROM mood_fragments")} == {a_id, b_id}
        assert conn.execute("SELECT DISTINCT config FROM profiles").fetchall() == [("{}",)]
        assert conn.execute("SELECT COUNT(*) FROM prompt_blocks").fetchone() == (0,)


def _as_file_before_profiles(path) -> None:
    """Walk an export of the 'default' profile's fragments back to a fragments-only file made before 0101."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        for table in _LIBRARY:
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "profile_id"]
            conn.execute(f"CREATE TABLE {table}_old AS SELECT {', '.join(columns)} FROM {table} WHERE profile_id = 'default'")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_old RENAME TO {table}")
        conn.execute("ALTER TABLE worlds ADD COLUMN is_global INTEGER NOT NULL DEFAULT 0")
        conn.execute("DROP TABLE profile_worlds")
        conn.execute("DELETE FROM profiles")
        conn.execute("ALTER TABLE profiles RENAME TO prompt_layouts")
        conn.execute("ALTER TABLE settings RENAME COLUMN active_profile_id TO active_prompt_layout_id")
        conn.execute("DELETE FROM schema_migrations WHERE id = '0101_profiles'")
        conn.execute("DROP TABLE orb_preset_meta")
        conn.execute(
            "CREATE TABLE orb_preset_meta (id INTEGER PRIMARY KEY CHECK (id = 1), included_domains TEXT NOT NULL,"
            " created_at TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,"
            " keys_stripped INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO orb_preset_meta VALUES (1, '[\"fragments\"]', '2026-01-01', 'old', 'manual', 0)")
        conn.commit()


async def test_an_old_partial_fragments_file_upgrades_and_applies(client, db_path):
    fid = (await client.get_json("/api/fragments"))[0]["id"]
    original = await _label(client, fid)
    name = (await client.post_json("/api/backups/export", json={"domains": ["fragments"]}))["name"]
    _as_file_before_profiles(db_path.parent / "snapshots" / name)
    [entry] = [e for e in await client.get_json("/api/backups") if e["name"] == name]
    assert entry["profiles"] == [{"id": "default", "name": "Default", "parts": ["fragments"]}]
    await client.put_checked(f"/api/fragments/{fid}", json={"label": "Edited"})

    await client.post_checked(f"/api/backups/{name}/apply", json={})
    assert await _label(client, fid) == original
    assert (await client.get_json("/api/profiles"))["profiles"] == [{"id": "default", "name": "Default"}]


# The tables as 0097 created them, which 0101 renames and extends.
_PRE_0101 = (
    "CREATE TABLE prompt_layouts (id TEXT PRIMARY KEY, name TEXT NOT NULL)",
    """CREATE TABLE prompt_blocks (
        id TEXT PRIMARY KEY,
        layout_id TEXT NOT NULL REFERENCES prompt_layouts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'system',
        region TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE mood_fragments (
        id TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT NOT NULL, prompt_text TEXT NOT NULL,
        negative_prompt TEXT NOT NULL DEFAULT '', cooldown_turns INTEGER NOT NULL DEFAULT 0, enabled BOOLEAN NOT NULL DEFAULT 1
    )""",
    "CREATE TABLE phrase_bank (id INTEGER PRIMARY KEY AUTOINCREMENT, variants TEXT NOT NULL, kind TEXT NOT NULL, pattern TEXT)",
)


def _pre_0101_database() -> sqlite3.Connection:
    """Today's schema walked back to its shape before 0101, holding two layouts with the second active, a fragment switched
    off, a phrase, and a global World."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(CREATE_TABLES_SQL)
    for table in ("prompt_blocks", "profiles", "profile_worlds", "mood_fragments", "phrase_bank"):
        conn.execute(f"DROP TABLE {table}")
    for ddl in _PRE_0101:
        conn.execute(ddl)
    conn.execute("ALTER TABLE settings RENAME COLUMN active_profile_id TO active_prompt_layout_id")
    conn.execute("ALTER TABLE worlds ADD COLUMN is_global INTEGER NOT NULL DEFAULT 0")
    conn.execute("INSERT INTO endpoints (id, url, active_model_config_id) VALUES (3, 'http://e/v1', NULL)")
    conn.execute("INSERT INTO model_configs (id, endpoint_id, model_name, role) VALUES (5, 3, 'm', 'writer')")
    conn.execute("UPDATE endpoints SET active_model_config_id = 5 WHERE id = 3")
    conn.execute(
        "INSERT INTO settings (id, active_endpoint_id, length_guard_max_words, enabled_tools, active_prompt_layout_id)"
        " VALUES (1, 3, 321, '{\"direct_scene\": false}', 'second')"
    )
    conn.execute("INSERT INTO mood_fragments (id, label, description, prompt_text, enabled) VALUES ('calm', 'Calm', '', '', 0)")
    conn.execute("INSERT INTO phrase_bank (variants, kind) VALUES ('[\"a beat\"]', 'literal')")
    conn.execute("INSERT INTO worlds (id, name, is_global, created_at, updated_at) VALUES ('w', 'W', 1, 't', 't')")
    conn.executemany("INSERT INTO prompt_layouts (id, name) VALUES (?, ?)", [("default", "Default"), ("second", "Second")])
    conn.executemany(
        "INSERT INTO prompt_blocks (id, layout_id, kind, region) VALUES (?, ?, 'main_prompt', 'prefix')",
        [("default:main_prompt", "default"), ("second:main_prompt", "second")],
    )
    conn.commit()
    return conn


def test_migration_turns_layouts_into_profiles_each_with_its_own_library():
    conn = _pre_0101_database()

    _MIGRATION.migrate(conn)
    _MIGRATION.migrate(conn)

    assert conn.execute("SELECT id, name FROM profiles ORDER BY id").fetchall() == [
        ("default", "Default"),
        ("second", "Second"),
    ]
    assert conn.execute("SELECT active_profile_id FROM settings").fetchone() == ("second",)
    lanes = conn.execute(
        "SELECT DISTINCT writer_endpoint_id, writer_model_config_id, agent_endpoint_id, judge_endpoint_id FROM profiles"
    ).fetchall()
    assert lanes == [(3, 5, None, None)]
    configs = [json.loads(config) for (config,) in conn.execute("SELECT config FROM profiles")]
    assert len(configs) == 2 and configs[0] == configs[1]
    assert configs[0]["settings"]["length_guard_max_words"] == 321
    assert configs[0]["settings"]["enabled_tools"] == {"direct_scene": False}
    assert configs[0]["models"] == {"agent_same_as_writer": 1, "judge_model": "typesafe/jev-1.13"}
    assert "fragments" not in configs[0]
    assert conn.execute("SELECT profile_id, id, enabled FROM mood_fragments ORDER BY profile_id").fetchall() == [
        ("default", "calm", 0),
        ("second", "calm", 0),
    ]
    assert conn.execute("SELECT profile_id, variants FROM phrase_bank ORDER BY profile_id").fetchall() == [
        ("default", '["a beat"]'),
        ("second", '["a beat"]'),
    ]
    assert conn.execute("SELECT profile_id, world_id FROM profile_worlds ORDER BY profile_id").fetchall() == [
        ("default", "w"),
        ("second", "w"),
    ]
    assert "is_global" not in {r[1] for r in conn.execute("PRAGMA table_info(worlds)")}

    assert {row[2] for row in conn.execute("PRAGMA foreign_key_list(prompt_blocks)")} == {"profiles"}
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM endpoints WHERE id = 3")
    assert conn.execute("SELECT DISTINCT writer_endpoint_id, writer_model_config_id FROM profiles").fetchall() == [(None, None)]
    conn.execute("DELETE FROM profiles WHERE id = 'second'")
    assert conn.execute("SELECT id FROM prompt_blocks").fetchall() == [("default:main_prompt",)]
    assert conn.execute("SELECT DISTINCT profile_id FROM mood_fragments").fetchall() == [("default",)]


def test_migration_finishes_a_database_that_has_global_presets():
    """Before the fold, a database could stop with a presets table whose configs held their own fragment switches."""
    conn = _pre_0101_database()
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("ALTER TABLE prompt_layouts RENAME TO presets")
    conn.execute("ALTER TABLE presets ADD COLUMN config TEXT NOT NULL DEFAULT '{}'")
    conn.execute("ALTER TABLE settings RENAME COLUMN active_prompt_layout_id TO active_preset_id")
    switches = {"default": True, "second": False}
    conn.executemany(
        "UPDATE presets SET config = ? WHERE id = ?",
        [(json.dumps({"models": {}, "fragments": {"mood": {"calm": on}}}), pid) for pid, on in switches.items()],
    )

    _MIGRATION.migrate(conn)
    _MIGRATION.migrate(conn)

    assert conn.execute("SELECT active_profile_id FROM settings").fetchone() == ("second",)
    # 'default' was left with the fragment on; 'second' is active, so its copy takes the live value.
    assert conn.execute("SELECT profile_id, id, enabled FROM mood_fragments ORDER BY profile_id").fetchall() == [
        ("default", "calm", 1),
        ("second", "calm", 0),
    ]
    # The maps leave the configs, and the lane picks are named (none here).
    assert [json.loads(c) for (c,) in conn.execute("SELECT config FROM profiles")] == [{"models": {}, "lanes": {}}] * 2
