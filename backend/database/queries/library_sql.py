"""Run model-written SELECTs over an allowlisted, read-only library projection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from .. import connection
from ..models import LibraryQueryResult, LibrarySqlCell

# Qualify sources with ``main.`` so each temporary view does not read itself.
_VIEWS: dict[str, str] = {
    "conversations": """
        SELECT c.id, c.title, c.character_card_id, c.character_name,
               COALESCE(c.persona_lock_id, card.persona_lock_id,
                        (SELECT active_persona_id FROM main.settings WHERE id = 1)) AS persona_id,
               c.kind, c.created_at, c.updated_at, c.last_accessed_at, c.active_leaf_id
        FROM main.conversations AS c
        LEFT JOIN main.character_cards AS card ON card.id = c.character_card_id""",
    "messages": """
        SELECT id, conversation_id, role, content, parent_id, turn_index, created_at
        FROM main.messages""",
    "characters": """
        SELECT id, name, description, personality, scenario, first_mes, mes_example, creator_notes,
               tags, alternate_greetings, creator, source_format, created_at, updated_at
        FROM main.character_cards""",
    "user_personas": """
        SELECT id, name, description, created_at, updated_at
        FROM main.user_personas""",
}
_TABLE_FUNCTIONS = frozenset({"json_each", "json_tree"})
_REFUSED_FUNCTIONS = frozenset({"load_extension"})
_MAX_LENGTH = 1_000_000


def _allowed_read(
    table: str,
    column: str,
    db: str | None,
    source: str | None,
    view_reads: frozenset[tuple[str, str]],
    schema_names: frozenset[str],
) -> bool:
    if db == "temp":
        return table in _VIEWS
    if table in _TABLE_FUNCTIONS:
        return True
    if column:
        return source in _VIEWS and (table, column) in view_reads
    # Handle count(*) and similar column-less reads conservatively.
    name = table.lower()
    return any(table == read for read, _ in view_reads) or (
        db is None and not name.startswith("sqlite_") and name not in schema_names
    )


def _authorizer(view_reads: frozenset[tuple[str, str]], schema_names: frozenset[str]):
    def authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _REFUSED_FUNCTIONS else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and _allowed_read(arg1 or "", arg2 or "", db, source, view_reads, schema_names):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def _record_view_reads(conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    """Every ``(table, column)`` the views read, as SQLite reports expanding them."""
    reads: set[tuple[str, str]] = set()

    def record(action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None) -> int:
        if action == sqlite3.SQLITE_READ and source in _VIEWS:
            reads.add((arg1 or "", arg2 or ""))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(record)
    for name in _VIEWS:
        conn.execute(f"SELECT * FROM {name} LIMIT 0")
    return frozenset(reads)


def _open(deadline: float) -> sqlite3.Connection:
    uri = Path(connection.DB_PATH).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        for name, body in _VIEWS.items():
            conn.execute(f"CREATE TEMP VIEW {name} AS {body}")
        schema = conn.execute("SELECT lower(name) FROM main.sqlite_schema").fetchall()
        view_reads = _record_view_reads(conn)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_LENGTH)
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        conn.set_authorizer(_authorizer(view_reads, frozenset(name for (name,) in schema)))
    except BaseException:
        conn.close()
        raise
    return conn


def _cell(value: object, cap: int) -> LibrarySqlCell:
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    text = str(value)
    return f"{text[:cap]}…[+{len(text) - cap} chars]" if len(text) > cap else text


def _run(sql: str, max_rows: int, max_cell_chars: int, max_result_chars: int, time_limit_s: float) -> LibraryQueryResult:
    conn = _open(time.monotonic() + time_limit_s)
    try:
        cursor = conn.execute(sql)
        columns = [column[0] for column in cursor.description or ()]
        fetched = cursor.fetchmany(max_rows + 1)
        more_rows = len(fetched) > max_rows
        rows: list[list[LibrarySqlCell]] = []
        size = len(json.dumps(columns, ensure_ascii=False))
        for raw in fetched[:max_rows]:
            row = [_cell(value, max_cell_chars) for value in raw]
            size += len(json.dumps(row, ensure_ascii=False)) + 1
            if rows and size > max_result_chars:
                more_rows = True
                break
            rows.append(row)
        return {"columns": columns, "rows": rows, "more_rows": more_rows}
    except (sqlite3.Error, sqlite3.Warning) as exc:
        message = str(exc)
        if message == "interrupted":
            message = f"query stopped after the {time_limit_s:g} s time limit; narrow it or add a LIMIT"
        elif message == "not authorized":
            message = "not authorized: read only the documented views and columns with a single SELECT"
        return {"error": message}
    finally:
        conn.close()


async def run_library_query(
    sql: str,
    *,
    max_rows: int,
    max_cell_chars: int,
    max_result_chars: int,
    time_limit_s: float,
) -> LibraryQueryResult:
    """Run one model-written SELECT and return SQL errors as data."""
    return await asyncio.to_thread(_run, sql, max_rows, max_cell_chars, max_result_chars, time_limit_s)
