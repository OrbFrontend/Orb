"""Order decisions among the Director fragments by when they run, and drop ``decision_placement``.

Decisions and Director fragments share one ordered stage, and its order is the run order. Each before-Director decision
moves ahead of the Director fragments and each after-Director one after them, every kind keeping its own order. They trade
the slots the stage's rows already hold, so no fragment of another stage moves. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of the stage rule here: every field type outside state and the Editor's runs with the Director.
_OTHER_STAGES = ("state", "feedback", "recast", "rewrite")


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if "decision_placement" not in columns:
        return
    rows = conn.execute(
        "SELECT id, field_type, sort_order, decision_placement FROM interactive_fragments ORDER BY sort_order, id"
    ).fetchall()
    stage = [row for row in rows if row[1] not in _OTHER_STAGES]
    after = [row for row in stage if row[1] == "decision" and row[3] == "after_director"]
    before = [row for row in stage if row[1] == "decision" and row[3] != "after_director"]
    director = [row for row in stage if row[1] != "decision"]
    asked = iter([*before, *director, *after])
    named = {row[0] for row in stage}
    order = [next(asked)[0] if row[0] in named else row[0] for row in rows]
    slots = [int(row[2]) for row in rows]
    for index in range(1, len(slots)):
        slots[index] = max(slots[index], slots[index - 1] + 1)
    held = {row[0]: row[2] for row in rows}
    moved = [(slot, fid) for fid, slot in zip(order, slots, strict=True) if held[fid] != slot]
    conn.executemany("UPDATE interactive_fragments SET sort_order = ? WHERE id = ?", moved)
    conn.execute("ALTER TABLE interactive_fragments DROP COLUMN decision_placement")
    conn.commit()
    print(f"[migrations] 0099: decisions ordered among the Director fragments ({len(moved)} moved), decision_placement dropped")
