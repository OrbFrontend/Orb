"""Global presets: leaving a preset saves the live state into it, entering one makes it live, a Backups apply keeps its
lanes, and migration 0101 turns the prompt layouts into presets."""

import importlib
import json
import sqlite3

from backend.database import get_active_layout_blocks, get_preset
from backend.database.schema import CREATE_TABLES_SQL

_migration = importlib.import_module("backend.database.migrations.0101_global_presets")


async def _live(client) -> dict:
    """The parts of the live state a preset carries, as the test changes them."""
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


async def test_switching_round_trips_each_preset_and_skips_a_deleted_lane(client):
    a_id = (await client.get_json("/api/presets"))["active_id"]
    second = await client.post_json("/api/endpoints", json={"url": "http://second/v1"})
    b_id = (await client.post_json("/api/presets", json={"name": "B"}))["id"]
    baseline = await _live(client)

    # Under B: the second endpoint writes.
    await client.put_checked("/api/presets/active", json={"id": b_id})
    await client.put_checked("/api/settings", json={"active_endpoint_id": second["id"]})
    assert (await client.put_json("/api/presets/active", json={"id": a_id}))["active_endpoint_id"] == baseline[
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

    row = await client.put_json("/api/presets/active", json={"id": b_id})
    assert row["active_preset_id"] == b_id and "api_key" not in row
    assert await _live(client) == {
        **baseline,
        "writer_endpoint": second["id"],
        "writer_config": second["active_model_config_id"],
        "model_name": "default",
    }
    assert {block.id.split(":")[0] for block in await get_active_layout_blocks() if ":" in block.id} == {b_id}

    await client.put_checked("/api/presets/active", json={"id": a_id})
    assert await _live(client) == edited
    assert edited["model_name"] == "a-model"

    # B's writer endpoint is gone while B is inactive: B's other values still apply, and the lane keeps A's endpoint.
    await client.delete_checked(f"/api/endpoints/{second['id']}")
    await client.put_checked("/api/presets/active", json={"id": b_id})
    assert await _live(client) == {**baseline, "writer_config": config["id"], "model_name": "a-model"}


async def test_a_preset_without_a_judge_turns_the_judge_off(client):
    a_id = (await client.get_json("/api/presets"))["active_id"]
    judge = await client.post_json("/api/endpoints", json={"url": "http://judge/v1", "kind": "judge"})
    await client.put_checked("/api/decisions/config", json={"decision_endpoint_id": None})
    b_id = (await client.post_json("/api/presets", json={"name": "No Judge"}))["id"]
    await client.put_checked("/api/decisions/config", json={"decision_endpoint_id": judge["id"]})

    assert (await client.put_json("/api/presets/active", json={"id": b_id}))["decision_endpoint_id"] is None
    assert (await client.put_json("/api/presets/active", json={"id": a_id}))["decision_endpoint_id"] == judge["id"]


async def test_a_backups_apply_renumbers_preset_lanes_with_the_endpoints(client):
    a_id = (await client.get_json("/api/presets"))["active_id"]
    await client.post_json("/api/endpoints", json={"url": "http://second/v1"})
    second = next(e for e in await client.get_json("/api/endpoints") if e["url"] == "http://second/v1")
    b_id = (await client.post_json("/api/presets", json={"name": "B"}))["id"]
    await client.put_checked("/api/presets/active", json={"id": b_id})
    await client.put_checked("/api/settings", json={"active_endpoint_id": second["id"]})
    await client.put_checked("/api/presets/active", json={"id": a_id})
    name = (await client.post_json("/api/backups/export", json={"domains": ["configs"], "strip_keys": False}))["name"]
    # Made after the snapshot, so the apply keeps it while replacing the endpoints it names.
    c_id = (await client.post_json("/api/presets", json={"name": "C"}))["id"]

    await client.post_checked(f"/api/backups/{name}/apply", json={})

    renumbered = next(e for e in await client.get_json("/api/endpoints") if e["url"] == "http://second/v1")
    assert renumbered["id"] != second["id"]
    assert (await client.put_json("/api/presets/active", json={"id": b_id}))["active_endpoint_id"] == renumbered["id"]
    assert (await client.get_json(f"/api/endpoints/{renumbered['id']}"))["active_model_config_id"] == renumbered[
        "active_model_config_id"
    ]
    c = await get_preset(c_id)
    assert c is not None and c["writer_endpoint_id"] is None and c["writer_model_config_id"] is None


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
)


def _pre_0101_database() -> sqlite3.Connection:
    """Today's schema walked back to its shape before 0101, holding two layouts with the second active."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(CREATE_TABLES_SQL)
    conn.execute("DROP TABLE prompt_blocks")
    conn.execute("DROP TABLE presets")
    for ddl in _PRE_0101:
        conn.execute(ddl)
    conn.execute("ALTER TABLE settings RENAME COLUMN active_preset_id TO active_prompt_layout_id")
    conn.execute("INSERT INTO endpoints (id, url, active_model_config_id) VALUES (3, 'http://e/v1', NULL)")
    conn.execute("INSERT INTO model_configs (id, endpoint_id, model_name, role) VALUES (5, 3, 'm', 'writer')")
    conn.execute("UPDATE endpoints SET active_model_config_id = 5 WHERE id = 3")
    conn.execute(
        "INSERT INTO settings (id, active_endpoint_id, length_guard_max_words, enabled_tools, active_prompt_layout_id)"
        " VALUES (1, 3, 321, '{\"direct_scene\": false}', 'second')"
    )
    conn.execute("INSERT INTO mood_fragments (id, label, description, prompt_text, enabled) VALUES ('calm', 'Calm', '', '', 0)")
    conn.executemany("INSERT INTO prompt_layouts (id, name) VALUES (?, ?)", [("default", "Default"), ("second", "Second")])
    conn.executemany(
        "INSERT INTO prompt_blocks (id, layout_id, kind, region) VALUES (?, ?, 'main_prompt', 'prefix')",
        [("default:main_prompt", "default"), ("second:main_prompt", "second")],
    )
    conn.commit()
    return conn


def test_migration_0101_turns_layouts_into_presets_with_the_live_state():
    conn = _pre_0101_database()

    _migration.migrate(conn)
    _migration.migrate(conn)

    assert conn.execute("SELECT id, name FROM presets ORDER BY id").fetchall() == [("default", "Default"), ("second", "Second")]
    assert conn.execute("SELECT active_preset_id FROM settings").fetchone() == ("second",)
    lanes = conn.execute(
        "SELECT DISTINCT writer_endpoint_id, writer_model_config_id, agent_endpoint_id, judge_endpoint_id FROM presets"
    ).fetchall()
    assert lanes == [(3, 5, None, None)]
    configs = [json.loads(config) for (config,) in conn.execute("SELECT config FROM presets")]
    assert len(configs) == 2 and configs[0] == configs[1]
    assert configs[0]["settings"]["length_guard_max_words"] == 321
    assert configs[0]["settings"]["enabled_tools"] == {"direct_scene": False}
    assert configs[0]["models"] == {"agent_same_as_writer": 1, "judge_model": "typesafe/jev-1.13"}
    assert configs[0]["fragments"]["mood"] == {"calm": 0}

    assert {row[2] for row in conn.execute("PRAGMA foreign_key_list(prompt_blocks)")} == {"presets"}
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM endpoints WHERE id = 3")
    assert conn.execute("SELECT DISTINCT writer_endpoint_id, writer_model_config_id FROM presets").fetchall() == [(None, None)]
    conn.execute("DELETE FROM presets WHERE id = 'second'")
    assert conn.execute("SELECT id FROM prompt_blocks").fetchall() == [("default:main_prompt",)]
