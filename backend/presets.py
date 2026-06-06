"""Preset / backup engine: selective export, merge-import, full snapshots.

A *preset* is a standalone SQLite ``.db`` file holding a chosen subset of the
live database's data plus an ``orb_preset_meta`` row describing what it carries.
Snapshots are the same kind of file (a full-domain preset) and live in the same
on-disk library, so the UI lists everything uniformly.

Two ways to bring a file's data into the live DB:

  * **apply** -- merge by identity. UUID-keyed entities (characters, worlds,
    conversations, fragments) upsert; a parent's child collection (a chat's
    message tree, a world's lorebook entries) is replaced wholesale; integer-PK
    rows that are *referenced* by other tables are reinserted with fresh ids and
    the references translated. Existing data the preset doesn't mention is left
    alone.
  * **restore** -- roll the live DB back to the file. A *full-coverage* file is
    swapped in whole (``restore_full``). A *partial* file is restored
    domain-scoped (``restore_partial``): the same merge machinery as apply, but
    each covered domain is emptied first, so those domains end up *exactly*
    matching the file (rows added since are dropped) while domains the file
    doesn't carry are left untouched.

All logic here is synchronous ``sqlite3`` (mirroring the migration runner) so it
can ``ATTACH`` databases and run ``VACUUM INTO``; routes invoke it via
``asyncio.to_thread`` while holding ``backend.locks.maintenance_lock``.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3

from .database.migrations import MIGRATIONS, run_pending

META_TABLE = "orb_preset_meta"

# Domain -> tables it owns. Order within a domain is informational; the merge
# order across domains is fixed in apply_preset().
DOMAIN_TABLES: dict[str, list[str]] = {
    "characters": ["character_cards"],
    "chats": [
        "conversations",
        "messages",
        "director_state",
        "conversation_logs",
        "user_attachments",
        "workflow_attachments",
    ],
    "lorebooks": ["worlds", "lorebook_entries"],
    "fragments": ["mood_fragments", "director_fragments"],
    "phrase_bank": ["phrase_bank"],
    "configs": ["settings", "endpoints", "model_configs", "user_personas"],
}
ALL_DOMAINS = list(DOMAIN_TABLES.keys())


class PresetError(Exception):
    """Raised for caller-facing preset failures (bad file, version skew, etc.)."""


# ── paths ───────────────────────────────────────────────────────────────────


def _db_path() -> str:
    # Resolved dynamically so tests that monkeypatch connection.DB_PATH work.
    from .database import connection

    return connection.DB_PATH


def _snapshots_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(_db_path())), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def _library_path(name: str) -> str:
    """Resolve a library entry by file name, rejecting path traversal.

    ``name`` arrives straight from a request path parameter, so it is validated
    against a strict filename allowlist before reaching any filesystem sink. The
    pattern admits only the characters our own ``_unique_name`` emits (letters,
    digits, ``-``, ``_``) plus the literal ``.db`` suffix, so ``/``, ``\\`` and
    any ``.`` outside that suffix -- hence every path separator and ``..`` -- are
    rejected up front. The repetition is length-bounded (a real name's stem is a
    15-char timestamp plus a slug capped at 40) so the match cannot backtrack on
    a long run of ``-`` (CodeQL ``py/polynomial-redos``).

    The resolved path is then normalised with ``realpath`` and checked to be
    strictly contained in the library root before it reaches any filesystem
    sink. This realpath-normalise-then-prefix-check is the barrier CodeQL
    ``py/path-injection`` recognises, and it doubles as the runtime guard against
    a symlink planted in the library (``realpath`` follows links, so an entry
    pointing outside the root fails the containment test). A validated ``name``
    is always a non-empty file name, so the resolved path is necessarily a child
    of the root -- never the root itself -- hence the plain ``startswith`` test.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.db", name):
        raise PresetError("Invalid preset name")
    root = os.path.realpath(_snapshots_dir())
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep):
        raise PresetError("Invalid preset name")
    if not os.path.isfile(path):
        raise PresetError("Preset not found")
    return path


def _unique_name(kind: str, label: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")[:40] if label else ""
    base = f"{ts}-{slug or kind}"
    name = f"{base}.db"
    i = 1
    while os.path.exists(os.path.join(_snapshots_dir(), name)):
        name = f"{base}-{i}.db"
        i += 1
    return name


# ── meta ──────────────────────────────────────────────────────────────────


def _write_meta(conn: sqlite3.Connection, included: list[str], label: str, kind: str, keys_stripped: bool) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  included_domains TEXT NOT NULL,"
        "  created_at TEXT NOT NULL,"
        "  label TEXT NOT NULL DEFAULT '',"
        "  kind TEXT NOT NULL,"
        "  keys_stripped INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(f"DELETE FROM {META_TABLE}")
    conn.execute(
        f"INSERT INTO {META_TABLE} (id, included_domains, created_at, label, kind, keys_stripped) " "VALUES (1, ?, ?, ?, ?, ?)",
        (json.dumps(sorted(included)), datetime.datetime.now().isoformat(timespec="seconds"), label, kind, int(keys_stripped)),
    )


def _stamp_migrations(conn: sqlite3.Connection) -> None:
    """Mark every current migration as applied.

    A preset we build is always cloned from the live DB, whose schema is current
    by definition, so the preset's schema is current too. Stamping the full set
    makes that explicit and keeps ``check_and_upgrade`` a no-op on our own files
    (``run_pending`` is not safe to re-run against an already-current schema).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  id TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    for mid in MIGRATIONS:
        conn.execute("INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)", (mid,))


def read_meta(path: str) -> dict | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            f"SELECT included_domains, created_at, label, kind, keys_stripped FROM {META_TABLE} WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    return {
        "included_domains": json.loads(row[0]),
        "created_at": row[1],
        "label": row[2],
        "kind": row[3],
        "keys_stripped": bool(row[4]),
    }


# ── small sql helpers ───────────────────────────────────────────────────────


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _upsert(conn: sqlite3.Connection, table: str) -> int:
    """INSERT OR REPLACE every preset row into main, keyed by the table's PK."""
    cols = _cols(conn, table)
    collist = ",".join(cols)
    ph = ",".join("?" * len(cols))
    rows = conn.execute(f"SELECT {collist} FROM preset.{table}").fetchall()
    for row in rows:
        conn.execute(f"INSERT OR REPLACE INTO main.{table} ({collist}) VALUES ({ph})", row)
    return len(rows)


def _insert_no_id(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    """Insert preset rows into main, dropping the autoincrement ``id``."""
    cols = [c for c in _cols(conn, table) if c != "id"]
    collist = ",".join(cols)
    ph = ",".join("?" * len(cols))
    rows = conn.execute(f"SELECT {collist} FROM preset.{table} {where}").fetchall()
    for row in rows:
        conn.execute(f"INSERT INTO main.{table} ({collist}) VALUES ({ph})", row)
    return len(rows)


# ── export ──────────────────────────────────────────────────────────────────


def _scrub_configs(conn: sqlite3.Connection) -> None:
    """Strip personal config + secrets when 'configs' is not exported.

    Deleting endpoints/personas auto-nulls their references on the settings row
    via the schema's ON DELETE SET NULL (and cascades model_configs). The free
    text fields are blanked so a shared preset never leaks prompts or identity.
    Import ignores configs in such a preset anyway (gated by meta), so exact
    default values do not matter -- only that nothing personal remains.
    """
    conn.execute("DELETE FROM endpoints")  # cascades model_configs; SET NULL on settings refs
    conn.execute("DELETE FROM user_personas")  # SET NULL on settings.active_persona_id
    conn.execute(
        "UPDATE settings SET api_key = '', user_name = 'User', user_description = '', "
        "system_prompt = '', shared_system_prompt = '', agent_shared_system_prompt = ''"
    )


def build_preset(selected_domains, strip_keys: bool, label: str = "") -> str:
    """Clone the live DB, prune unselected domains, tag with meta, store in the
    library. Returns the on-disk file name."""
    kind = "manual"  # always a user-initiated snapshot; auto/import are tagged elsewhere
    selected = set(selected_domains)
    unknown = selected - set(ALL_DOMAINS)
    if unknown:
        raise PresetError(f"Unknown domains: {sorted(unknown)}")
    if "chats" in selected:
        selected.add("characters")  # chats are meaningless without their character
    if not selected:
        raise PresetError("Select at least one domain to export")

    tmp = os.path.join(_snapshots_dir(), f".build-{os.getpid()}-{datetime.datetime.now():%H%M%S%f}.tmp")
    src = sqlite3.connect(_db_path())
    try:
        src.execute("VACUUM INTO ?", (tmp,))
    finally:
        src.close()

    keys_stripped = False
    c = sqlite3.connect(tmp, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys=ON")
        if "chats" not in selected:
            c.execute("DELETE FROM conversations")  # cascades messages/logs/attachments/director_state
        if "characters" not in selected:
            c.execute("DELETE FROM character_cards")
        if "lorebooks" not in selected:
            c.execute("DELETE FROM worlds")  # cascades lorebook_entries; SET NULL character_cards.world_id
        if "fragments" not in selected:
            c.execute("DELETE FROM mood_fragments")
            c.execute("DELETE FROM director_fragments")
        if "phrase_bank" not in selected:
            c.execute("DELETE FROM phrase_bank")
        if "configs" not in selected:
            _scrub_configs(c)
        elif strip_keys:
            c.execute("UPDATE settings SET api_key = ''")
            c.execute("UPDATE endpoints SET api_key = ''")
            keys_stripped = True
        _stamp_migrations(c)
        _write_meta(c, sorted(selected), label, kind, keys_stripped)
        c.execute("VACUUM")  # reclaim pages freed by the deletes
    finally:
        c.close()

    name = _unique_name(kind, label)
    shutil.move(tmp, os.path.join(_snapshots_dir(), name))
    return name


# ── merge (apply) ─────────────────────────────────────────────────────────


def _merge_lorebooks(conn: sqlite3.Connection) -> None:
    _upsert(conn, "worlds")
    for (wid,) in conn.execute("SELECT id FROM preset.worlds").fetchall():
        conn.execute("DELETE FROM main.lorebook_entries WHERE world_id = ?", (wid,))
    _insert_no_id(conn, "lorebook_entries")


def _merge_characters(conn: sqlite3.Connection) -> None:
    _upsert(conn, "character_cards")
    # Drop links to worlds that aren't present locally (FK would otherwise dangle).
    conn.execute(
        "UPDATE main.character_cards SET world_id = NULL "
        "WHERE world_id IS NOT NULL AND world_id NOT IN (SELECT id FROM main.worlds)"
    )


def _merge_chats(conn: sqlite3.Connection) -> None:
    conv_ids = [r[0] for r in conn.execute("SELECT id FROM preset.conversations").fetchall()]
    if not conv_ids:
        return

    # 1. Replace each conversation wholesale. apply runs with foreign_keys=OFF,
    #    so ON DELETE CASCADE does not fire -- clear the old subtree by hand
    #    (child rows first) or the previous messages/logs/attachments survive
    #    alongside the freshly imported ones.
    conv_cols = _cols(conn, "conversations")
    ali = conv_cols.index("active_leaf_id")
    collist = ",".join(conv_cols)
    ph = ",".join("?" * len(conv_cols))
    conv_ph = ",".join("?" * len(conv_ids))
    old_msgs = f"SELECT id FROM main.messages WHERE conversation_id IN ({conv_ph})"
    conn.execute(f"DELETE FROM main.workflow_attachments WHERE message_id IN ({old_msgs})", conv_ids)
    conn.execute(f"DELETE FROM main.user_attachments WHERE message_id IN ({old_msgs})", conv_ids)
    conn.execute(f"DELETE FROM main.conversation_logs WHERE conversation_id IN ({conv_ph})", conv_ids)
    conn.execute(f"DELETE FROM main.director_state WHERE conversation_id IN ({conv_ph})", conv_ids)
    conn.execute(f"DELETE FROM main.messages WHERE conversation_id IN ({conv_ph})", conv_ids)
    conn.execute(f"DELETE FROM main.conversations WHERE id IN ({conv_ph})", conv_ids)
    for row in conn.execute(f"SELECT {collist} FROM preset.conversations").fetchall():
        vals = list(row)
        vals[ali] = None  # set after messages exist
        conn.execute(f"INSERT INTO main.conversations ({collist}) VALUES ({ph})", vals)

    # 2. Messages: remap integer ids, inserting parents before children.
    msg_cols = _cols(conn, "messages")
    id_i = msg_cols.index("id")
    par_i = msg_cols.index("parent_id")
    ins_cols = [c for c in msg_cols if c != "id"]
    ins_par = ins_cols.index("parent_id")
    ins_sql = f"INSERT INTO main.messages ({','.join(ins_cols)}) VALUES ({','.join('?' * len(ins_cols))})"
    rows = conn.execute(f"SELECT {','.join(msg_cols)} FROM preset.messages").fetchall()
    msg_map: dict[int, int] = {}
    pending = list(rows)
    progressed = True
    while pending and progressed:
        progressed = False
        still = []
        for r in pending:
            parent = r[par_i]
            if parent is None or parent in msg_map:
                vals = [r[msg_cols.index(c)] for c in ins_cols]
                vals[ins_par] = msg_map[parent] if parent is not None else None
                cur = conn.execute(ins_sql, vals)
                assert cur.lastrowid is not None
                msg_map[r[id_i]] = cur.lastrowid
                progressed = True
            else:
                still.append(r)
        pending = still
    for r in pending:  # orphaned/cyclic parent: attach to root
        vals = [r[msg_cols.index(c)] for c in ins_cols]
        vals[ins_par] = None
        cur = conn.execute(ins_sql, vals)
        assert cur.lastrowid is not None
        msg_map[r[id_i]] = cur.lastrowid

    # 3. director_state keyed by conversation_id (cleared above).
    _insert_no_id_keep_all(conn, "director_state")

    # 4. conversation_logs: drop id, remap nullable message_id.
    _insert_remap_message(conn, "conversation_logs", msg_map, nullable=True)

    # 5. user_attachments: drop id, remap NOT NULL message_id.
    _insert_remap_message(conn, "user_attachments", msg_map, nullable=False)

    # 6. workflow_attachments: drop id, remap message_id + self-refs (two-pass).
    _merge_workflow_attachments(conn, msg_map)

    # 7. Point each conversation at its remapped active leaf.
    for (cid,) in [(c,) for c in conv_ids]:
        leaf = conn.execute("SELECT active_leaf_id FROM preset.conversations WHERE id = ?", (cid,)).fetchone()
        old = leaf[0] if leaf else None
        if old is not None and old in msg_map:
            conn.execute("UPDATE main.conversations SET active_leaf_id = ? WHERE id = ?", (msg_map[old], cid))


def _insert_no_id_keep_all(conn: sqlite3.Connection, table: str) -> None:
    """Copy rows whose PK is not autoincrement (e.g. director_state)."""
    cols = _cols(conn, table)
    collist = ",".join(cols)
    ph = ",".join("?" * len(cols))
    for row in conn.execute(f"SELECT {collist} FROM preset.{table}").fetchall():
        conn.execute(f"INSERT OR REPLACE INTO main.{table} ({collist}) VALUES ({ph})", row)


def _insert_remap_message(conn: sqlite3.Connection, table: str, msg_map: dict[int, int], nullable: bool) -> None:
    cols = [c for c in _cols(conn, table) if c != "id"]
    mi = cols.index("message_id")
    ph = ",".join("?" * len(cols))
    for row in conn.execute(f"SELECT {','.join(cols)} FROM preset.{table}").fetchall():
        vals = list(row)
        old = vals[mi]
        if old is None:
            new = None
        elif old in msg_map:
            new = msg_map[old]
        else:
            if not nullable:
                continue  # message wasn't imported; drop the orphan attachment/log
            new = None
        vals[mi] = new
        conn.execute(f"INSERT INTO main.{table} ({','.join(cols)}) VALUES ({ph})", vals)


def _merge_workflow_attachments(conn: sqlite3.Connection, msg_map: dict[int, int]) -> None:
    table = "workflow_attachments"
    all_cols = _cols(conn, table)
    id_i = all_cols.index("id")
    cols = [c for c in all_cols if c != "id"]
    mi = cols.index("message_id")
    par_i = cols.index("parent_attachment_id")
    sib_i = cols.index("active_sibling_id")
    ph = ",".join("?" * len(cols))
    attach_map: dict[int, int] = {}
    deferred: list[tuple[int, int | None, int | None]] = []  # (new_id, old_parent, old_sibling)
    for row in conn.execute(f"SELECT {','.join(all_cols)} FROM preset.{table}").fetchall():
        old_id = row[id_i]
        vals = [row[all_cols.index(c)] for c in cols]
        old_msg = vals[mi]
        if old_msg not in msg_map:
            continue  # message not imported
        vals[mi] = msg_map[old_msg]
        old_parent, old_sib = vals[par_i], vals[sib_i]
        vals[par_i] = None
        vals[sib_i] = None
        cur = conn.execute(f"INSERT INTO main.{table} ({','.join(cols)}) VALUES ({ph})", vals)
        assert cur.lastrowid is not None
        attach_map[old_id] = cur.lastrowid
        deferred.append((cur.lastrowid, old_parent, old_sib))
    for new_id, old_parent, old_sib in deferred:
        conn.execute(
            f"UPDATE main.{table} SET parent_attachment_id = ?, active_sibling_id = ? WHERE id = ?",
            (
                attach_map.get(old_parent) if old_parent is not None else None,
                attach_map.get(old_sib) if old_sib is not None else None,
                new_id,
            ),
        )


def _merge_configs(conn: sqlite3.Connection) -> None:
    # Preserve attachment-cache bookkeeping across the settings overwrite (see
    # the rationale in bootstrap.reset_to_defaults).
    cur = conn.execute(
        "SELECT attachment_cache_budget_bytes, attachment_access_counter FROM main.settings WHERE id = 1"
    ).fetchone()

    # apply runs with foreign_keys=OFF, so deleting endpoints does NOT cascade to
    # model_configs -- clear them by hand or the old rows are left orphaned (their
    # endpoint gone), which trips the foreign_key_check at the end of apply.
    conn.execute("DELETE FROM main.model_configs")
    conn.execute("DELETE FROM main.endpoints")
    conn.execute("DELETE FROM main.user_personas")

    # personas
    persona_map: dict[int, int] = {}
    p_cols = _cols(conn, "user_personas")
    p_id = p_cols.index("id")
    p_ins = [c for c in p_cols if c != "id"]
    p_ph = ",".join("?" * len(p_ins))
    for row in conn.execute(f"SELECT {','.join(p_cols)} FROM preset.user_personas").fetchall():
        vals = [row[p_cols.index(c)] for c in p_ins]
        new = conn.execute(f"INSERT INTO main.user_personas ({','.join(p_ins)}) VALUES ({p_ph})", vals).lastrowid
        assert new is not None
        persona_map[row[p_id]] = new

    # endpoints first, with model-config back-refs nulled
    endpoint_map: dict[int, int] = {}
    e_cols = _cols(conn, "endpoints")
    e_id = e_cols.index("id")
    e_ins = [c for c in e_cols if c != "id"]
    e_amc = e_ins.index("active_model_config_id")
    e_agmc = e_ins.index("agent_active_model_config_id")
    e_ph = ",".join("?" * len(e_ins))
    for row in conn.execute(f"SELECT {','.join(e_cols)} FROM preset.endpoints").fetchall():
        vals = [row[e_cols.index(c)] for c in e_ins]
        vals[e_amc] = None
        vals[e_agmc] = None
        new = conn.execute(f"INSERT INTO main.endpoints ({','.join(e_ins)}) VALUES ({e_ph})", vals).lastrowid
        assert new is not None
        endpoint_map[row[e_id]] = new

    # model_configs with remapped endpoint_id
    mc_map: dict[int, int] = {}
    m_cols = _cols(conn, "model_configs")
    m_id = m_cols.index("id")
    m_ins = [c for c in m_cols if c != "id"]
    m_ep = m_ins.index("endpoint_id")
    m_ph = ",".join("?" * len(m_ins))
    for row in conn.execute(f"SELECT {','.join(m_cols)} FROM preset.model_configs").fetchall():
        vals = [row[m_cols.index(c)] for c in m_ins]
        vals[m_ep] = endpoint_map.get(row[m_cols.index("endpoint_id")])
        new = conn.execute(f"INSERT INTO main.model_configs ({','.join(m_ins)}) VALUES ({m_ph})", vals).lastrowid
        assert new is not None
        mc_map[row[m_id]] = new

    # fix endpoint -> model_config back-refs
    for row in conn.execute("SELECT id, active_model_config_id, agent_active_model_config_id FROM preset.endpoints").fetchall():
        conn.execute(
            "UPDATE main.endpoints SET active_model_config_id = ?, agent_active_model_config_id = ? WHERE id = ?",
            (mc_map.get(row[1]), mc_map.get(row[2]), endpoint_map[row[0]]),
        )

    # settings: overwrite the singleton, remapping its FK refs, keeping cache cols
    s_cols = _cols(conn, "settings")
    ps = conn.execute(f"SELECT {','.join(s_cols)} FROM preset.settings WHERE id = 1").fetchone()
    if ps:
        sets, vals = [], []
        for i, c in enumerate(s_cols):
            if c in ("id", "attachment_cache_budget_bytes", "attachment_access_counter"):
                continue
            v = ps[i]
            if c in ("active_endpoint_id", "agent_endpoint_id"):
                v = endpoint_map.get(v) if v is not None else None
            elif c == "active_persona_id":
                v = persona_map.get(v) if v is not None else None
            sets.append(f"{c} = ?")
            vals.append(v)
        conn.execute(f"UPDATE main.settings SET {', '.join(sets)} WHERE id = 1", vals)
        if cur is not None:
            conn.execute(
                "UPDATE main.settings SET attachment_cache_budget_bytes = ?, attachment_access_counter = ? WHERE id = 1",
                (cur[0], cur[1]),
            )


# Domains whose apply-merge is additive (upsert / per-parent replace). A
# domain-scoped *restore* must empty these before merging so the file's rows
# land in an empty domain and the domain ends up exactly matching the file.
# `configs` (overwrites the settings singleton in place) and `phrase_bank`
# (its merge already deletes first) are full replacements on apply already, so
# they are deliberately absent.
_REPLACE_WIPE_DOMAINS = ("characters", "chats", "lorebooks", "fragments")


def _replace_wipe(conn: sqlite3.Connection, included: set[str]) -> None:
    """Empty each covered additive domain ahead of its merge (restore only).

    Deletes child-first (``reversed(DOMAIN_TABLES)``) to stay correct even if
    foreign keys are ever on; apply runs FK-off, so the final
    ``foreign_key_check`` is what actually guards the committed state.
    """
    for domain in _REPLACE_WIPE_DOMAINS:
        if domain in included:
            for table in reversed(DOMAIN_TABLES[domain]):
                conn.execute(f"DELETE FROM main.{table}")


def apply_preset(preset_path: str, *, replace: bool = False) -> dict:
    """Merge a preset's data into the live DB by identity. Returns row counts
    per merged domain. Raises PresetError on schema-version skew or FK failure.

    With ``replace=True`` (the partial-restore path) each covered domain is
    emptied before its merge, so the domain ends up exactly matching the file
    rather than merged into existing rows; domains the file doesn't carry are
    left untouched."""
    check_and_upgrade(preset_path)
    included = set(preset_domains(preset_path))

    conn = sqlite3.connect(_db_path(), isolation_level=None)
    summary: dict[str, int] = {}
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS preset", (preset_path,))
        conn.execute("BEGIN")
        if replace:
            _replace_wipe(conn, included)
        if "lorebooks" in included:
            _merge_lorebooks(conn)
            summary["lorebooks"] = conn.execute("SELECT COUNT(*) FROM preset.worlds").fetchone()[0]
        if "fragments" in included:
            _upsert(conn, "mood_fragments")
            _upsert(conn, "director_fragments")
            summary["fragments"] = (
                conn.execute("SELECT COUNT(*) FROM preset.mood_fragments").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM preset.director_fragments").fetchone()[0]
            )
        if "characters" in included:
            _merge_characters(conn)
            summary["characters"] = conn.execute("SELECT COUNT(*) FROM preset.character_cards").fetchone()[0]
        if "chats" in included:
            _merge_chats(conn)
            summary["chats"] = conn.execute("SELECT COUNT(*) FROM preset.conversations").fetchone()[0]
        if "phrase_bank" in included:
            conn.execute("DELETE FROM main.phrase_bank")
            _insert_no_id(conn, "phrase_bank")
            summary["phrase_bank"] = conn.execute("SELECT COUNT(*) FROM preset.phrase_bank").fetchone()[0]
        if "configs" in included:
            _merge_configs(conn)
            summary["configs"] = 1

        if replace and "lorebooks" in included:
            # Worlds were replaced wholesale; null any character link to a world
            # the file didn't carry. (When characters was also covered,
            # _merge_characters already ran this; harmless to repeat.)
            conn.execute(
                "UPDATE main.character_cards SET world_id = NULL "
                "WHERE world_id IS NOT NULL AND world_id NOT IN (SELECT id FROM main.worlds)"
            )

        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            conn.execute("ROLLBACK")
            raise PresetError(f"Import would corrupt foreign keys ({len(problems)} violations); aborted.")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        try:
            conn.execute("DETACH DATABASE preset")
        except sqlite3.OperationalError:
            pass
        conn.close()
    return summary


def restore_partial(preset_path: str) -> dict:
    """Roll the covered domains back to a partial file (domain-scoped restore).

    Replaces each domain the file carries to match it exactly and leaves the
    rest untouched. The full-coverage counterpart is ``restore_full``.
    """
    return apply_preset(preset_path, replace=True)


# ── snapshots / restore / library ─────────────────────────────────────────


def create_snapshot(label: str = "") -> str:
    """Take an automatic full-clone backup of the live DB into the library,
    pruned to a bounded count. Used before destructive ops (import/apply/restore)."""
    name = _unique_name("auto", label)
    dest = os.path.join(_snapshots_dir(), name)
    src = sqlite3.connect(_db_path())
    try:
        src.execute("VACUUM INTO ?", (dest,))
    finally:
        src.close()
    c = sqlite3.connect(dest, isolation_level=None)
    try:
        _stamp_migrations(c)
        _write_meta(c, ALL_DOMAINS, label, "auto", False)
    finally:
        c.close()
    prune_auto()
    return name


def restore_full(name: str) -> None:
    """Replace the live DB file with a library file (clean rollback).

    The replacement is prepared out-of-place and swapped in with an atomic
    ``os.replace``. We never write to (or delete the WAL/SHM of) the live path
    while it is open: the running app serves overlapping requests on their own
    short-lived connections, and overwriting the file or removing its ``-wal``
    out from under one leaves it holding a lock, which made the prep writes
    below fail with "database is locked". After the swap, any still-open
    connection keeps running on the now-unlinked old inode and finishes
    cleanly, while new connections see the restored file.

    The swapped-in file is in rollback (DELETE) journal mode, but the *old*
    live DB ran in WAL mode, so its ``-wal``/``-shm`` are still sitting at the
    live path beside the freshly restored file. Clearing the ``-wal`` is NOT
    cosmetic: SQLite replays a ``-wal`` it finds next to a database on open
    *regardless of that database's own journal mode* (verified on 3.45), so a
    surviving stale ``-wal`` is recovered over the restored file and silently
    reverts the restore to the previous database's contents (a truncated one
    can instead leave a malformed file). We therefore treat the removal as
    mandatory and raise if the ``-wal`` cannot be cleared, rather than letting
    a latent revert surface on the next restart.

    Residual race, not closed here: a reader that opens the live path in the
    brief window between the ``os.replace`` and the removal below can itself
    replay the stale ``-wal``. Fully closing it means gating ``get_db`` opens
    for the duration of the swap (a process-wide reader/writer barrier), which
    is out of scope for this file-swap helper.
    """
    src = _library_path(name)
    live = _db_path()
    tmp = f"{live}.restore-{os.getpid()}"
    shutil.copyfile(src, tmp)
    try:
        # Drop the preset marker so it doesn't ride along in the live DB, and
        # bring the file up to the current schema -- all on the temp copy, which
        # nothing else has open.
        conn = sqlite3.connect(tmp, isolation_level=None)
        try:
            conn.execute(f"DROP TABLE IF EXISTS {META_TABLE}")
        finally:
            conn.close()
        run_pending(tmp)
        os.replace(tmp, live)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    # Clear the previous inode's WAL/SHM left at the live path. Removing the
    # -wal is mandatory (see above): SQLite would otherwise replay it over the
    # restored file on the next open and silently revert the restore. -shm
    # alone is inert, so its removal stays best-effort. On Linux the unlink
    # succeeds even while a finishing connection holds the file open; if the
    # -wal still cannot be cleared, fail loudly so the user retries instead of
    # discovering the revert only after a restart.
    for sfx in ("-wal", "-shm"):
        p = live + sfx
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    wal = live + "-wal"
    if os.path.exists(wal):
        raise PresetError(
            f"Restore finished but a stale WAL file could not be cleared ({os.path.basename(wal)}); "
            "the database may revert on the next restart. Close other connections and restore again."
        )


def list_library() -> list[dict]:
    out = []
    for fn in os.listdir(_snapshots_dir()):
        if not fn.endswith(".db") or fn.startswith("."):
            continue
        path = os.path.join(_snapshots_dir(), fn)
        meta = read_meta(path) or {}
        st = os.stat(path)
        out.append(
            {
                "name": fn,
                "label": meta.get("label", ""),
                "kind": meta.get("kind", "unknown"),
                "included_domains": meta.get("included_domains", []),
                "keys_stripped": meta.get("keys_stripped", False),
                "created_at": meta.get("created_at"),
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
        )
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def delete_library_entry(name: str) -> None:
    os.remove(_library_path(name))


def prune_auto(keep: int = 10) -> None:
    autos = sorted(
        (e for e in list_library() if e["kind"] == "auto"),
        key=lambda x: x["mtime"],
        reverse=True,
    )
    for entry in autos[keep:]:
        try:
            os.remove(os.path.join(_snapshots_dir(), entry["name"]))
        except OSError:
            pass


# ── import (external file) ─────────────────────────────────────────────────


def check_and_upgrade(path: str) -> None:
    """Validate a file is an Orb database and migrate it up to the current
    schema. Rejects files produced by a newer Orb build."""
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "settings" not in tables:
            raise PresetError("Not an Orb database file.")
        try:
            applied = {r[0] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
        except sqlite3.OperationalError:
            applied = set()
    except sqlite3.DatabaseError as e:
        raise PresetError(f"Not a valid SQLite database: {e}") from e
    finally:
        conn.close()

    unknown = applied - set(MIGRATIONS)
    if unknown:
        raise PresetError(
            "This preset was made with a newer version of Orb "
            f"(unknown migrations: {sorted(unknown)}). Update Orb to import it."
        )
    run_pending(path)


def preset_domains(path: str) -> list[str]:
    """Domains a file declares, or all domains for a raw (meta-less) Orb DB."""
    meta = read_meta(path)
    return meta["included_domains"] if meta else list(ALL_DOMAINS)


def ingest_upload(tmp_path: str, label: str) -> str:
    """Validate + upgrade an uploaded .db, tag it as an imported preset, and move
    it into the library. Returns the stored file name.

    The "imported" kind always wins over whatever the file was tagged as
    elsewhere (it may have been a "manual" snapshot in another Orb): from this
    library's point of view it arrived from outside. We keep the file's own
    domain coverage so the restore guard stays accurate for partial presets.
    """
    check_and_upgrade(tmp_path)
    existing = read_meta(tmp_path)
    included = existing["included_domains"] if existing else ALL_DOMAINS
    keys_stripped = existing["keys_stripped"] if existing else False
    label = existing["label"] if existing and existing["label"] else label
    c = sqlite3.connect(tmp_path, isolation_level=None)
    try:
        _write_meta(c, included, label, "imported", keys_stripped)
    finally:
        c.close()
    name = _unique_name("imported", label)
    dest = os.path.join(_snapshots_dir(), name)
    shutil.move(tmp_path, dest)
    return name
