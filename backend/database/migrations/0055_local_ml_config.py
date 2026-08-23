"""
0055_local_ml_config -- per-local-ML-feature config blob on ``settings``.

Sibling to ``local_ml_enabled``, which answers "is this feature on"; this one
answers "and how is it configured". The first tenant is the prose rewriter,
whose choices are which of three checkpoints to serve and whether to offload to
the GPU: ``{"prose_rewriter": {"variant": "4b-q8", "gpu": true}}``.

A JSON column rather than two more flat columns because the shape is the
feature's own business and a second variant-bearing feature must not need a
migration. Defaults to '{}', so an existing install reads every feature as
unconfigured -- which for the rewriter means "no model selected", and the
Editor pass simply does not run it.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if not cols:
        # No settings table at all. PRAGMA table_info is silent about that and
        # the ALTER would abort the whole chain, taking startup with it -- but
        # init_db runs straight after and creates the table from schema.py, with
        # this column already on it. Skipping is self-healing; crashing is not.
        return
    if "local_ml_config" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN local_ml_config TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0055: added local_ml_config column to settings")
