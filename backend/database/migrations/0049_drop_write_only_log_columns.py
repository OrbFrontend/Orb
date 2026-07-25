"""
0049_drop_write_only_log_columns -- drop the two write-only ``conversation_logs``
columns, ``agent_raw_output`` and ``progressive_fields_after``.

Both were persisted on every turn and never read back into behaviour:

* ``agent_raw_output`` -- one full raw Director response per turn. The Inspector
  projection never included it, the frontend has no reference to it, and the
  checkpoint copy only carried it forward so the next copy could also not read
  it. The raw response is still logged to the application log at generation
  time, which is where anyone debugging a Director turn actually looks.
* ``progressive_fields_after`` -- a per-turn snapshot of the progressive field
  values. The pipeline deliberately reads those from the message tree instead
  (``progressive.branch_baseline``), because ``conversation_logs`` is not
  branch-aware; the snapshot here was only ever copied to checkpoints.

Kept as one migration because they share a cause: a log row is written by the
pipeline, and columns leak into it more easily than they leave.
"""

from __future__ import annotations

import sqlite3

_WRITE_ONLY_COLUMNS = ("agent_raw_output", "progressive_fields_after")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    for col in _WRITE_ONLY_COLUMNS:
        if col in cols:
            conn.execute(f"ALTER TABLE conversation_logs DROP COLUMN {col}")
            print(f"[migrations] 0049: dropped write-only conversation_logs.{col}")
