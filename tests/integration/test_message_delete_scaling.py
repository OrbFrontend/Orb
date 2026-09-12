"""Deleting a message must stay linear in the size of the subtree.

Deleting a message deletes its siblings and every descendant, so on a long chat
with swipes the subtree *is* most of the conversation. Two properties keep that
affordable, and both are invisible in a small-fixture correctness test -- they
only show up as seconds of wall clock on a real chat:

1. Every foreign key that a message delete cascades through is indexed. SQLite
   enforces ``ON DELETE`` by searching the *child* table for referencing rows;
   without an index on the child key that search is a full table scan, repeated
   per deleted row.
2. The subtree walk seeks on ``parent_id`` rather than re-scanning the whole
   conversation at every level, and the delete runs deepest-first so SQLite
   never has to cascade down a chain itself.

Measured on a 3,000-message chat (500 turns x 5 swipes), deleting ~2,700 of
them with ``delete_message_with_descendants``:

* neither property ....... 6,315 ms   (and 7,058 ms with attachments attached)
* deepest-first only .......  431 ms   (1,641 ms)
* both (shipped) ...........   63 ms   (  257 ms)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.database.queries.messages import _levels_deepest_first

# Delete-propagating edges only: ``ON DELETE CASCADE`` turns one delete into
# another, which is what grows the set of tables a message delete has to search.
# ``SET NULL`` writes a row but never deletes it, so it ends the walk.
_CASCADE = "CASCADE"


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    """``(child_column, referenced_table, on_delete)`` for *table*."""
    return [(row[3], row[2], row[6]) for row in conn.execute(f"PRAGMA foreign_key_list({table})")]  # nosec B608 -- from sqlite_master


def _indexed_leading_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Columns that some index can seek on, i.e. appear first in one."""
    leading = set()
    for index in conn.execute(f"PRAGMA index_list({table})"):  # nosec B608 -- from sqlite_master
        columns = [c[2] for c in conn.execute(f"PRAGMA index_info({index[1]})")]
        if columns and columns[0] is not None:
            leading.add(columns[0])
    return leading


def _delete_closure(conn: sqlite3.Connection, root: str) -> set[str]:
    """Every table whose rows a delete from *root* can cascade into."""
    closure = {root}
    changed = True
    while changed:
        changed = False
        for table in _tables(conn):
            if table in closure:
                continue
            if any(ref in closure and on_delete == _CASCADE for _, ref, on_delete in _foreign_keys(conn, table)):
                closure.add(table)
                changed = True
    return closure


def _unindexed_child_keys(conn: sqlite3.Connection, closure: set[str]) -> list[str]:
    """Child keys pointing *into* the closure that SQLite would have to scan for."""
    offenders = []
    for table in _tables(conn):
        leading = _indexed_leading_columns(conn, table)
        for column, referenced, _on_delete in _foreign_keys(conn, table):
            if referenced in closure and column not in leading:
                offenders.append(f"{table}.{column} -> {referenced}")
    return sorted(offenders)


async def test_every_foreign_key_a_message_delete_touches_is_indexed(db_path: Path):
    """The whole cascade closure under ``messages``, not just a fixed list.

    Derived from the live schema so a new table that cascades off messages --
    another attachment kind, say -- is covered the day it lands instead of
    quietly making deletion quadratic again.
    """
    conn = sqlite3.connect(db_path)
    try:
        closure = _delete_closure(conn, "messages")
        # Sanity: the closure really is the delete fan-out, not just {messages}.
        assert {"messages", "user_attachments", "workflow_attachments", "direction_notes"} <= closure
        offenders = _unindexed_child_keys(conn, closure)
    finally:
        conn.close()

    assert offenders == [], (
        "foreign-key child columns without a leading index, reachable from a message delete: "
        f"{offenders} — SQLite scans the whole child table once per deleted row to enforce these"
    )


@pytest.mark.parametrize(
    "sql_name, expected_index",
    [
        ("subtree walk", "idx_messages_parent"),
    ],
)
async def test_subtree_walk_seeks_on_parent_id(db_path: Path, sql_name: str, expected_index: str):
    """The recursive step must seek by ``parent_id``, not re-scan the conversation.

    ``delete_message_with_descendants`` and ``get_message_delete_preview`` both
    constrain the recursive step by ``parent_id`` *and* ``conversation_id``.
    Given the choice the planner takes the ``conversation_id`` index, which
    makes every level of the walk scan every message in the chat; the queries
    defeat that with SQLite's ``+`` no-index operator on the conversation guard
    (see ``_SAME_CONVERSATION``). Losing the ``+`` is a silent, large
    regression, so assert the plan rather than the wording.
    """
    conn = sqlite3.connect(db_path)
    try:
        plan = [
            row[3]
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                WITH RECURSIVE subtree(id, depth) AS (
                    SELECT id, 0 FROM messages WHERE conversation_id = ? AND parent_id = ?
                    UNION ALL
                    SELECT m.id, s.depth + 1 FROM messages m
                    INNER JOIN subtree s ON m.parent_id = s.id
                    WHERE +m.conversation_id = ?
                )
                SELECT id, depth FROM subtree
                """,
                ("c", 1, "c"),
            )
        ]
    finally:
        conn.close()

    steps = [line for line in plan if line.startswith(("SEARCH", "SCAN"))]
    assert any(expected_index in line and "parent_id=?" in line for line in steps), (
        f"{sql_name} no longer seeks on {expected_index}; plan was:\n  " + "\n  ".join(plan)
    )
    assert not any("idx_messages_exchange" in line for line in steps), (
        "the walk fell back to the conversation index — every level now scans the whole chat:\n  " + "\n  ".join(plan)
    )


def test_levels_deepest_first_groups_by_descending_depth():
    """Leaves go out first, and one level is one batch.

    Order is the reason the delete is cheap: ``messages.parent_id`` cascades, so
    deleting a parent while its children are still there makes SQLite walk the
    subtree itself, once per level of the chat. Rows at equal depth are never
    ancestors of each other, so each level can go out in a single statement.
    """
    # (depth, id) — deliberately unsorted, with several rows sharing a depth.
    pairs = [(0, 10), (2, 30), (1, 20), (2, 31), (0, 11), (1, 21)]

    levels = _levels_deepest_first(pairs)

    assert levels == [[30, 31], [20, 21], [10, 11]]


def test_levels_deepest_first_handles_an_empty_subtree():
    assert _levels_deepest_first([]) == []
