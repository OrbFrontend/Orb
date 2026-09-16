"""Merge the Prose Rewriter's two on/off switches into its workflow toggle.

Before: ``local_ml_enabled.prose_rewriter`` turned the engine (and the manual
rewrite button) on, and ``workflow_enabled.prose_rewriter`` turned the automatic
pass on. After: the workflow toggle turns the rewriter on, and the workflow's
``automatic`` config decides whether new replies run through it.

With a model selected the old engine switch becomes the workflow toggle and the
old workflow toggle becomes ``automatic``. Without one neither switch did
anything, so the workflow toggle keeps its meaning and ``automatic`` starts at
its default. Keyed on the absence of the ``prose_rewriter`` config slot, so it
runs once.
"""

from __future__ import annotations

import json
import sqlite3

FEATURE = "prose_rewriter"


def _map(raw: object) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if not {"workflow_config", "workflow_enabled", "local_ml_enabled", "local_ml_config"} <= cols:
        return
    row = conn.execute(
        "SELECT workflow_config, workflow_enabled, local_ml_enabled, local_ml_config FROM settings WHERE id = 1"
    ).fetchone()
    if row is None:
        return
    workflow_config, workflow_enabled, local_ml_enabled, local_ml_config = (_map(value) for value in row)
    if FEATURE in workflow_config:
        return

    selection = local_ml_config.get(FEATURE)
    engine_on = local_ml_enabled.pop(FEATURE, True) is not False
    automatic = True
    if isinstance(selection, dict) and selection.get("variant"):
        automatic = workflow_enabled.get(FEATURE, True) is not False
        workflow_enabled[FEATURE] = engine_on
    workflow_config[FEATURE] = {"automatic": automatic}

    conn.execute(
        "UPDATE settings SET workflow_config = ?, workflow_enabled = ?, local_ml_enabled = ? WHERE id = 1",
        (json.dumps(workflow_config), json.dumps(workflow_enabled), json.dumps(local_ml_enabled)),
    )
    print(
        f"[migrations] 0063: Prose Rewriter {'on' if workflow_enabled.get(FEATURE, True) else 'off'}, "
        f"automatic={'on' if automatic else 'off'}"
    )
