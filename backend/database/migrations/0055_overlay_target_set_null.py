"""0055_overlay_target_set_null -- stop an authored delete from destroying the overlay.

``lorebook_entries.supersedes_entry_id`` shipped in 0053 as ``ON DELETE
CASCADE``. That is the wrong action for what the column means: it points from an
Agent-managed overlay row at the *authored* row it hides, so deleting the
authored entry deleted the overlay too -- silently discarding lore the user had
already reviewed and accepted, with no changeset and no history record. The
natural cleanup after accepting a ``replace`` is to delete the now-redundant
authored entry, which made this the likely path rather than a corner case.

``ON DELETE SET NULL`` keeps the overlay row and drops only the pointer. The
projection already reads a dangling target correctly (see
``inference/lorebook.select_effective_entries``): a ``replace`` that no longer
hides anything is a standalone ``add``, and a ``suppress`` with nothing to
suppress injects nothing, exactly as before.

SQLite cannot alter a foreign key in place, so the table is rebuilt from the
canonical fresh-install DDL. A database whose column already says SET NULL --
every fresh install, and any DB already through this migration -- is skipped.
"""

from __future__ import annotations

import sqlite3

from backend.database import schema

_TABLE = "lorebook_entries"
_INDEXES = ("CREATE INDEX IF NOT EXISTS idx_lorebook_overlay ON lorebook_entries(world_id, entry_layer, archived);",)


def _already_current(conn: sqlite3.Connection) -> bool:
    """True when ``supersedes_entry_id`` already SET-NULLs, or predates 0053 entirely."""
    # PRAGMA foreign_key_list row: (id, seq, table, from, to, on_update, on_delete, match)
    edges = conn.execute(f"PRAGMA foreign_key_list({_TABLE})").fetchall()
    for row in edges:
        if row[3] == "supersedes_entry_id":
            return str(row[6]).upper() == "SET NULL"
    # No such edge: either the column is missing (0053 has not run) or it was
    # ALTER-added without an enforced REFERENCES clause. Nothing to rebuild.
    return True


def _rebuild(conn: sqlite3.Connection) -> None:
    block = schema.table_create_sql(_TABLE)
    new_ddl = block.replace(f"CREATE TABLE IF NOT EXISTS {_TABLE}", f"CREATE TABLE {_TABLE}_new", 1)
    conn.execute(new_ddl)
    # Intersect old and new columns rather than assuming they match: a DB that
    # reached here through the ALTER path may lack one the canonical DDL names.
    new_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_TABLE}_new)").fetchall()]
    old_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()}
    cols = ", ".join(c for c in new_cols if c in old_cols)
    conn.execute(f"INSERT INTO {_TABLE}_new ({cols}) SELECT {cols} FROM {_TABLE}")  # nosec B608 — names from the canonical schema
    conn.execute(f"DROP TABLE {_TABLE}")
    conn.execute(f"ALTER TABLE {_TABLE}_new RENAME TO {_TABLE}")
    for statement in _INDEXES:
        conn.execute(statement)


def migrate(conn: sqlite3.Connection) -> None:
    if _already_current(conn):
        return

    # Standard SQLite table-rebuild recipe (as in 0027 / 0054). The runner has
    # committed before calling us; PRAGMA foreign_keys is a no-op inside a
    # transaction, and enforcement during the rebuild would let DROP TABLE
    # cascade through the very self-reference this migration exists to defuse.
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        _rebuild(conn)
        conn.commit()
        print("[migrations] 0055: overlay supersedes_entry_id now SET NULL instead of CASCADE")
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
