from __future__ import annotations

import json
import sqlite3

from backend.features.presets.engine import _scrub_image_gen_workflow_config


def test_nested_image_gen_secrets_and_user_graphs_are_scrubbed():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, workflow_config TEXT)")
    config = {
        "image_gen": {
            "external_comfy": {
                "api_key": "KEY_CANARY",
                "user_graphs": [{"graph": {"1": {"inputs": {"token": "GRAPH_CANARY"}}}}],
                "styles": [{"id": "anime"}],
            }
        }
    }
    conn.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps(config),))
    _scrub_image_gen_workflow_config(conn)
    raw = conn.execute("SELECT workflow_config FROM settings").fetchone()[0]
    assert "KEY_CANARY" not in raw
    assert "GRAPH_CANARY" not in raw
    assert json.loads(raw)["image_gen"]["external_comfy"]["styles"] == [{"id": "anime"}]
