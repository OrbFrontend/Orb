"""Tests for migration 0063's merge of the Prose Rewriter's two switches."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

MODEL = {"prose_rewriter": {"variant": "4b-q8", "gpu": True, "batch_size": 4}}


def _migrate(conn: sqlite3.Connection) -> None:
    importlib.import_module("backend.database.migrations.0063_prose_rewriter_workflow_toggle").migrate(conn)


def _staged(*, workflow_enabled: dict, local_ml_enabled: dict, local_ml_config: dict, workflow_config=None):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE settings (id INTEGER PRIMARY KEY, workflow_config TEXT, workflow_enabled TEXT, "
        "local_ml_enabled TEXT, local_ml_config TEXT)"
    )
    conn.execute(
        "INSERT INTO settings VALUES (1, ?, ?, ?, ?)",
        (
            json.dumps(workflow_config or {"tts": {"auto_play": True}}),
            json.dumps(workflow_enabled),
            json.dumps(local_ml_enabled),
            json.dumps(local_ml_config),
        ),
    )
    return conn


def _row(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    raw = conn.execute("SELECT workflow_config, workflow_enabled, local_ml_enabled FROM settings").fetchone()
    return tuple(json.loads(value) for value in raw)  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("engine_on", "workflow_on", "expected_on", "expected_automatic"),
    [
        (True, True, True, True),
        (True, False, True, False),  # manual-only users keep their rewrite button
        (False, True, False, True),
        (False, False, False, False),
    ],
)
def test_a_configured_rewriter_swaps_the_engine_switch_into_the_workflow_toggle(
    engine_on, workflow_on, expected_on, expected_automatic
):
    conn = _staged(
        workflow_enabled={"prose_rewriter": workflow_on, "tts": False},
        local_ml_enabled={"prose_rewriter": engine_on, "autocomplete": False},
        local_ml_config=MODEL,
    )

    _migrate(conn)
    workflow_config, workflow_enabled, local_ml_enabled = _row(conn)

    assert workflow_enabled == {"prose_rewriter": expected_on, "tts": False}
    assert workflow_config == {"tts": {"auto_play": True}, "prose_rewriter": {"automatic": expected_automatic}}
    assert local_ml_enabled == {"autocomplete": False}


def test_missing_keys_mean_on_as_they_did_before():
    conn = _staged(workflow_enabled={}, local_ml_enabled={}, local_ml_config=MODEL)

    _migrate(conn)
    workflow_config, workflow_enabled, _ = _row(conn)

    assert workflow_enabled == {"prose_rewriter": True}
    assert workflow_config["prose_rewriter"] == {"automatic": True}


def test_without_a_model_the_workflow_toggle_keeps_its_meaning():
    conn = _staged(
        workflow_enabled={"prose_rewriter": False},
        local_ml_enabled={"prose_rewriter": True},
        local_ml_config={"prose_rewriter": {"variant": None, "gpu": True, "batch_size": 4}},
    )

    _migrate(conn)
    workflow_config, workflow_enabled, local_ml_enabled = _row(conn)

    assert workflow_enabled == {"prose_rewriter": False}
    assert workflow_config["prose_rewriter"] == {"automatic": True}
    assert local_ml_enabled == {}


def test_runs_once():
    conn = _staged(
        workflow_enabled={"prose_rewriter": False},
        local_ml_enabled={"prose_rewriter": True},
        local_ml_config=MODEL,
    )

    _migrate(conn)
    first = _row(conn)
    _migrate(conn)

    assert _row(conn) == first
