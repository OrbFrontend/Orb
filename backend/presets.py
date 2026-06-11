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

import dataclasses
import datetime
import json
import os
import re
import shutil
import sqlite3

from .database import preset_schema as ps
from .database.migrations import MIGRATIONS, run_pending

META_TABLE = "orb_preset_meta"

# The set of user-facing domains, derived from the declared roots. Order is
# informational (meta stores them sorted); the actual merge order is the
# schema-derived topological sort in _build_schema_model().
ALL_DOMAINS: list[str] = sorted(set(ps.DOMAIN_ROOTS.values()))


class PresetError(Exception):
    """Raised for caller-facing preset failures (bad file, version skew, etc.)."""


# ── schema model (derived from the live schema, zero hand-maintenance) ───────
#
# Everything the merge engine needs -- table classification, the FK graph, a
# safe insert order, which edges to defer -- is read from the live database with
# PRAGMA. Adding a table or an FK column therefore requires no edit here: the
# model simply grows. The only hand-declared inputs are the product/security
# policy in backend/database/preset_schema.py.


@dataclasses.dataclass
class _FK:
    """One foreign-key edge of a table, classified by what it means for a merge."""

    table: str  # the child table the column lives on
    from_col: str
    parent: str
    to_col: str
    on_delete: str  # 'CASCADE' | 'SET NULL' | 'NO ACTION' | ...
    notnull: bool  # is from_col declared NOT NULL?

    @property
    def is_self(self) -> bool:
        return self.parent == self.table

    @property
    def kind(self) -> str:
        # ownership = "this row is part of that entity" (deleting the parent
        # deletes the child); crossref = "soft pointer to another entity".
        if self.is_self:
            return "self"
        return "ownership" if self.on_delete == "CASCADE" else "crossref"


@dataclasses.dataclass
class _Table:
    name: str
    cols: list[str]
    pk: list[str]
    kind: str  # 'singleton' | 'stable' | 'surrogate'
    fks: list[_FK]
    owner_fk: _FK | None  # the single ownership (CASCADE, non-self) parent edge

    def fk(self, col: str) -> _FK | None:
        for f in self.fks:
            if f.from_col == col:
                return f
        return None


@dataclasses.dataclass
class _Schema:
    tables: dict[str, _Table]
    order: list[str]  # topological insert order (parents before children)
    deferred: set[tuple[str, str]]  # (table, from_col) edges set NULL on insert, fixed up after

    def root_of(self, table: str) -> _Table:
        """Climb ownership edges to the entity root (the table with no owner)."""
        t = self.tables[table]
        while t.owner_fk is not None:
            t = self.tables[t.owner_fk.parent]
        return t

    def domain_of(self, table: str) -> str | None:
        return ps.DOMAIN_ROOTS.get(self.root_of(table).name)

    def domain_tables(self, domain: str) -> list[str]:
        """Tables belonging to *domain*, in topological (parent-first) order."""
        return [t for t in self.order if self.domain_of(t) == domain]


_SINGLETON_RE = re.compile(r"check\s*\(\s*id\s*=\s*1\s*\)", re.IGNORECASE)


def _build_schema_model(conn: sqlite3.Connection) -> _Schema:
    """Introspect the live schema (the ``main`` database) into an in-memory model.

    Classification is read straight from PRAGMA + the stored DDL:
      * *singleton* -- a ``CHECK (id = 1)`` table (settings): updated in place.
      * *surrogate* -- a lone INTEGER primary key (an autoincrement rowid): its id
        is not portable, so rows reinsert under fresh ids with an old->new map.
      * *stable*    -- everything else (a TEXT primary key, or a PK that is itself
        a foreign key like director_state.conversation_id): identity is portable,
        so rows upsert by primary key.
    """
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    ddl = {name: (sql or "") for name, sql in rows}
    names = [n for n in ddl if n not in ps.EXCLUDED_TABLES and not n.startswith("sqlite_")]

    tables: dict[str, _Table] = {}
    for name in names:
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()
        cols = [r[1] for r in info]
        types = {r[1]: (r[2] or "").upper() for r in info}
        notnull = {r[1]: bool(r[3]) for r in info}
        pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]

        fks: list[_FK] = []
        for r in conn.execute(f"PRAGMA foreign_key_list({name})").fetchall():
            parent, from_col, to_col, on_delete = r[2], r[3], r[4], r[6]
            if to_col is None:  # implicit reference to the parent's primary key
                to_col = tables[parent].pk[0] if parent in tables else "id"
            fks.append(_FK(name, from_col, parent, to_col, on_delete, notnull.get(from_col, False)))

        if _SINGLETON_RE.search(ddl[name]):
            kind = "singleton"
        elif len(pk) == 1 and types.get(pk[0], "").startswith("INTEGER"):
            kind = "surrogate"
        else:
            kind = "stable"

        owner = next((f for f in fks if f.kind == "ownership"), None)
        tables[name] = _Table(name, cols, pk, kind, fks, owner)

    order, deferred = _topo_order(tables)
    return _Schema(tables, order, deferred)


def _topo_order(tables: dict[str, _Table]) -> tuple[list[str], set[tuple[str, str]]]:
    """Order tables so every non-deferred FK's parent is inserted before its child.

    Self edges are deferred from the start (a row references its own table, which
    cannot exist yet). Genuine cycles -- conversations.active_leaf_id <-> messages,
    endpoints.active_model_config_id <-> model_configs -- are broken by deferring a
    *crossref* edge inside the cycle (never an ownership edge, which defines the
    tree). Deferred columns are inserted NULL and fixed up once every id-map exists.
    """
    deferred: set[tuple[str, str]] = set()
    for t in tables.values():
        for f in t.fks:
            if f.is_self:
                deferred.add((t.name, f.from_col))

    placed: set[str] = set()
    order: list[str] = []
    while len(placed) < len(tables):
        progressed = False
        for name, t in tables.items():
            if name in placed:
                continue
            unmet = any(
                f.parent in tables and f.parent not in placed and not f.is_self and (name, f.from_col) not in deferred
                for f in t.fks
            )
            if not unmet:
                order.append(name)
                placed.add(name)
                progressed = True
        if progressed:
            continue
        # Stalled: a cycle remains. Break it by deferring one crossref edge whose
        # parent is still unplaced.
        broke = False
        for name, t in tables.items():
            if name in placed:
                continue
            for f in t.fks:
                if f.kind == "crossref" and f.parent not in placed and (name, f.from_col) not in deferred:
                    deferred.add((name, f.from_col))
                    broke = True
                    break
            if broke:
                break
        if not broke:
            raise PresetError("Unbreakable foreign-key cycle in schema")
    return order, deferred


def schema_coverage_problems(conn: sqlite3.Connection) -> list[str]:
    """Return human-readable reasons the live schema is not fully covered by the
    declared preset policy -- empty when everything is accounted for.

    The drift backstop: a new table that no DOMAIN_ROOT owns, a foreign key whose
    parent the engine never classified, or a secret-looking column missing from
    SECRET_COLUMNS each surfaces here (and fails the coverage test) the moment the
    schema changes, instead of silently dropping data or aborting an import later.
    """
    schema = _build_schema_model(conn)
    problems: list[str] = []
    for name, t in schema.tables.items():
        if schema.domain_of(name) is None:
            problems.append(
                f"table {name!r} reaches no DOMAIN_ROOT via ownership; assign its root "
                f"a domain in DOMAIN_ROOTS or add {name!r} to EXCLUDED_TABLES"
            )
        for fk in t.fks:
            if fk.parent not in schema.tables:
                problems.append(
                    f"{name}.{fk.from_col} references unclassified parent {fk.parent!r} " f"(excluded or unknown table)"
                )
        for col in t.cols:
            if ps.is_sensitive_column(col) and (name, col) not in ps.SECRET_COLUMNS:
                problems.append(
                    f"column {name}.{col} looks secret but is not in SECRET_COLUMNS; "
                    f"add it (with its scrub value) or rename it"
                )
    return problems


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


# ── export ──────────────────────────────────────────────────────────────────


def _scrub_configs(conn: sqlite3.Connection, schema: _Schema) -> None:
    """Strip personal config + secrets when 'configs' is not exported.

    Deleting the configs domain's non-singleton roots (endpoints, user_personas)
    auto-nulls their references on the settings row via the schema's ON DELETE SET
    NULL and cascades model_configs (this runs FK-on, on the export clone). The
    singleton's secret/free-text columns are then blanked per SECRET_COLUMNS so a
    shared preset never leaks a key, identity, or prompts. Import ignores configs
    in such a preset anyway (gated by meta), so exact default values do not matter
    -- only that nothing personal remains. Both the set of configs roots and the
    blanked columns are derived/declared, so a new configs table or secret column
    is covered without editing here.
    """
    for root, domain in ps.DOMAIN_ROOTS.items():
        if domain == "configs" and schema.tables[root].kind != "singleton":
            conn.execute(f"DELETE FROM {root}")
    for (table, col), blank in ps.SECRET_COLUMNS.items():
        if schema.tables[table].kind == "singleton":
            conn.execute(f"UPDATE {table} SET {col} = ?", (blank,))


def build_preset(selected_domains, strip_keys: bool, label: str = "") -> str:
    """Clone the live DB, prune unselected domains, tag with meta, store in the
    library. Returns the on-disk file name."""
    kind = "manual"  # always a user-initiated snapshot; auto/import are tagged elsewhere
    selected = set(selected_domains)
    unknown = selected - set(ALL_DOMAINS)
    if unknown:
        raise PresetError(f"Unknown domains: {sorted(unknown)}")
    for trigger, implied in ps.IMPLIED_DOMAINS.items():
        if trigger in selected:
            selected |= implied  # e.g. chats are meaningless without their character
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
        schema = _build_schema_model(c)
        # Prune each unselected domain by deleting its root tables: with FK on, a
        # CASCADE prunes the owned children and a SET NULL clears soft pointers, so
        # no per-child delete is hand-coded. configs is special (it scrubs the
        # singleton in place rather than deleting it).
        for domain in ALL_DOMAINS:
            if domain in selected:
                continue
            if domain == "configs":
                _scrub_configs(c, schema)
                continue
            for root, root_domain in ps.DOMAIN_ROOTS.items():
                if root_domain == domain and schema.tables[root].kind != "singleton":
                    c.execute(f"DELETE FROM {root}")
        if "configs" in selected and strip_keys:
            for table, col in ((t, col) for (t, col) in ps.SECRET_COLUMNS if col == "api_key"):
                c.execute(f"UPDATE {table} SET {col} = ''")
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
#
# One generic engine drives every domain. Given the schema model it:
#   A. (restore only) wipes each additive domain so it ends up matching the file.
#   B. clears the subtree each incoming entity replaces (child-replace scope).
#   C. inserts/upserts every covered table in topological order, dropping
#      surrogate ids (recording an old->new map) and rewriting FK columns.
#   D. fixes up deferred self/cycle back-pointers once every id-map exists.
#   E. reconciles soft pointers from *other* domains into any fully-replaced table.
# Adding a child table or an FK column needs no edit here -- the model grows and
# these passes pick it up.


def _existing(conn: sqlite3.Connection, cache: dict[str, set], parent: str, to_col: str) -> set:
    """Memoised set of a parent table's current key values in ``main``.

    Used for the "keep this value -- it still resolves locally" branch of the FK
    rewrite. A parent is always fully inserted/upserted before any child consults
    it (topological order), and parents are never re-touched afterwards, so the
    set is stable once built.
    """
    if parent not in cache:
        cache[parent] = {r[0] for r in conn.execute(f"SELECT {to_col} FROM main.{parent}")}
    return cache[parent]


def _resolve_fk(value, fk: _FK, idmaps: dict[str, dict[int, int]], conn, cache) -> tuple[object, bool]:
    """Translate one FK value for a row being merged. Returns ``(new_value, drop)``.

    The single rule that replaces every bespoke remap:
      * ``None`` stays ``None``.
      * if the parent was surrogate-remapped this merge, the value is portable
        only through that map -- in the map -> the new id; not in it -> dangling
        (its old surrogate id means nothing locally).
      * otherwise (stable/untouched parent) the value is portable as-is -> keep it
        if it still resolves in ``main``, else dangling.
      * a dangling value is dropped-as-NULL for a SET NULL / nullable column, or
        the whole child row is dropped for a NOT NULL ownership (CASCADE) column.
    """
    if value is None:
        return None, False
    pmap = idmaps.get(fk.parent)
    if pmap is not None:
        if value in pmap:
            return pmap[value], False
    elif value in _existing(conn, cache, fk.parent, fk.to_col):
        return value, False
    # dangling
    if fk.kind == "ownership" and fk.notnull:
        return None, True
    return None, False


def _scope_clause(schema: _Schema, table: str, root: str) -> str:
    """A WHERE clause selecting ``main.table`` rows owned by the *incoming* roots.

    Walks the ownership chain up from ``table`` to ``root``, building nested
    subqueries: the final hop targets ``preset.root`` (the entities being
    re-imported), the intermediate hops join through ``main``. This generalises
    the hand-written "delete this conversation's message tree" prune.
    """
    fk = schema.tables[table].owner_fk
    assert fk is not None
    if fk.parent == root:
        return f"{fk.from_col} IN (SELECT {fk.to_col} FROM preset.{root})"
    inner = _scope_clause(schema, fk.parent, root)
    return f"{fk.from_col} IN (SELECT {fk.to_col} FROM main.{fk.parent} WHERE {inner})"


def _merge_table(conn, schema, table, idmaps, cache) -> None:
    """Insert/upsert one covered table, rewriting FKs and recording its id-map."""
    t = schema.tables[table]
    cols = t.cols
    deferred = {c for (tbl, c) in schema.deferred if tbl == table}
    fks = {f.from_col: f for f in t.fks}

    if t.kind == "singleton":
        # Update the lone row in place; never insert/delete it. PRESERVED_COLUMNS
        # keep their local values (cache bookkeeping, not config from the file).
        pk = t.pk[0]
        keep = set(t.pk) | set(ps.PRESERVED_COLUMNS.get(table, ()))
        row = conn.execute(f"SELECT {','.join(cols)} FROM preset.{table} WHERE {pk} = 1").fetchone()
        if row is None:
            return
        sets, vals = [], []
        for c, v in zip(cols, row):
            if c in keep:
                continue
            if c in fks:
                v, _ = _resolve_fk(v, fks[c], idmaps, conn, cache)
            sets.append(f"{c} = ?")
            vals.append(v)
        conn.execute(f"UPDATE main.{table} SET {', '.join(sets)} WHERE {pk} = 1", vals)
        return

    if t.kind == "stable":
        # Identity is portable: upsert by primary key (the child-replace in
        # phase B already cleared any subtree this row owns).
        ph = ",".join("?" * len(cols))
        for row in conn.execute(f"SELECT {','.join(cols)} FROM preset.{table}").fetchall():
            vals = list(row)
            for i, c in enumerate(cols):
                if c in deferred:
                    vals[i] = None  # fixed up once the referenced rows exist
                elif c in fks:
                    vals[i], _ = _resolve_fk(vals[i], fks[c], idmaps, conn, cache)
            conn.execute(f"INSERT OR REPLACE INTO main.{table} ({','.join(cols)}) VALUES ({ph})", vals)
        return

    # surrogate: reinsert dropping the autoincrement id, record old->new.
    (pk,) = t.pk
    ins_cols = [c for c in cols if c != pk]
    ph = ",".join("?" * len(ins_cols))
    idmap: dict[int, int] = {}
    for row in conn.execute(f"SELECT {','.join(cols)} FROM preset.{table}").fetchall():
        rowd = dict(zip(cols, row))
        vals, drop = [], False
        for c in ins_cols:
            v = rowd[c]
            if c in deferred:
                v = None
            elif c in fks:
                v, drop = _resolve_fk(v, fks[c], idmaps, conn, cache)
                if drop:
                    break
            vals.append(v)
        if drop:
            continue  # an owning parent did not survive the import; drop the orphan
        new = conn.execute(f"INSERT INTO main.{table} ({','.join(ins_cols)}) VALUES ({ph})", vals).lastrowid
        assert new is not None
        idmap[rowd[pk]] = new
    idmaps[table] = idmap


def _fixup_deferred(conn, schema, table, from_col, idmaps, cache) -> None:
    """Resolve a deferred (self or cycle) FK column once every id-map exists.

    The column was inserted NULL; now translate the file's original value through
    the same rule and write it back, keyed by the row's new identity. Covers
    messages.parent_id, the workflow-attachment self refs, conversations'
    active_leaf_id, and the endpoints<->model_configs back-pointers in one pass.
    """
    t = schema.tables[table]
    fk = t.fk(from_col)
    assert fk is not None
    pk = t.pk[0]
    own_map = idmaps.get(table)  # surrogate tables only
    for row in conn.execute(f"SELECT {pk}, {from_col} FROM preset.{table}").fetchall():
        old_pk, old_val = row[0], row[1]
        new_pk = own_map[old_pk] if own_map is not None else old_pk
        if own_map is not None and old_pk not in own_map:
            continue  # row was dropped during insert
        new_val, _ = _resolve_fk(old_val, fk, idmaps, conn, cache)
        conn.execute(f"UPDATE main.{table} SET {from_col} = ? WHERE {pk} = ?", (new_val, new_pk))


def _reconcile_crossref(conn, schema, fk: _FK, idmaps, cache) -> None:
    """Realign every row of a soft-pointer column after its parent was *fully*
    replaced, including rows the import never touched.

    A full table replace (re-keying user_personas, or wiping worlds on a restore)
    can orphan pointers held by rows in *other* domains -- a pre-existing
    character's persona_lock_id, a stale world link. This is the generalised
    successor to _reconcile_persona_locks and the world_id null-out: remap through
    the parent's old->new map where one exists, then NULL whatever still dangles.
    (Same-domain children are not reconciled here: the domain's own replace already
    rebuilt them, and their surrogate parent ids are not portable across it.)
    """
    table, col = fk.table, fk.from_col
    pmap = idmaps.get(fk.parent)
    if pmap:
        conn.execute("CREATE TEMP TABLE _fk_remap (old INTEGER PRIMARY KEY, new INTEGER)")
        conn.executemany("INSERT INTO _fk_remap (old, new) VALUES (?, ?)", list(pmap.items()))
        conn.execute(
            f"UPDATE main.{table} SET {col} = (SELECT new FROM _fk_remap WHERE old = {col}) "
            f"WHERE {col} IN (SELECT old FROM _fk_remap)"
        )
        conn.execute("DROP TABLE _fk_remap")
    conn.execute(
        f"UPDATE main.{table} SET {col} = NULL "
        f"WHERE {col} IS NOT NULL AND {col} NOT IN (SELECT {fk.to_col} FROM main.{fk.parent})"
    )


def _merge(conn: sqlite3.Connection, included: set[str], replace: bool) -> dict[str, int]:
    schema = _build_schema_model(conn)
    inc = [t for t in schema.order if schema.domain_of(t) in included]
    fully_replaced: set[str] = set()

    # A. Restore only: empty each additive domain (one whose entity root is a
    #    stable-key table that apply merges by upsert) so it ends up matching the
    #    file exactly. Domains with no stable root -- configs (singleton + replaced
    #    surrogate roots), phrase_bank -- are already full replacements on apply, so
    #    they are left to phase B.
    if replace:
        for domain in included:
            roots = [r for r, d in ps.DOMAIN_ROOTS.items() if d == domain]
            if roots and all(schema.tables[r].kind == "stable" for r in roots):
                for table in reversed(schema.domain_tables(domain)):
                    conn.execute(f"DELETE FROM main.{table}")
                    fully_replaced.add(table)

    # B. Child-replace: clear the subtree each incoming entity supersedes, child
    #    first. A table whose entity root is stable is replaced per-root (scoped to
    #    the incoming ids); the stable root itself is left for the upsert. A table
    #    whose root is surrogate (endpoints/model_configs, user_personas,
    #    phrase_bank) has no portable identity, so it is wiped wholesale.
    for table in reversed(inc):
        t = schema.tables[table]
        if t.kind == "singleton" or table in fully_replaced:
            continue
        root = schema.root_of(table)
        if root.kind == "stable":
            if table != root.name:
                conn.execute(f"DELETE FROM main.{table} WHERE {_scope_clause(schema, table, root.name)}")
        else:
            conn.execute(f"DELETE FROM main.{table}")
            fully_replaced.add(table)

    # C. Insert/upsert in topological order so every parent precedes its children.
    cache: dict[str, set] = {}
    idmaps: dict[str, dict[int, int]] = {}
    for table in inc:
        _merge_table(conn, schema, table, idmaps, cache)

    # D. Fix up deferred self/cycle back-pointers now that every id-map exists.
    for table, col in schema.deferred:
        if schema.domain_of(table) in included:
            _fixup_deferred(conn, schema, table, col, idmaps, cache)

    # E. Reconcile cross-domain soft pointers into any fully-replaced parent.
    for t in schema.tables.values():
        for fk in t.fks:
            if (
                fk.kind == "crossref"
                and (t.name, fk.from_col) not in schema.deferred
                and fk.parent in fully_replaced
                and schema.domain_of(t.name) != schema.domain_of(fk.parent)
            ):
                _reconcile_crossref(conn, schema, fk, idmaps, cache)

    # Row counts per merged domain (configs, anchored on its singleton, reports 1).
    summary: dict[str, int] = {}
    for domain in included:
        roots = [r for r, d in ps.DOMAIN_ROOTS.items() if d == domain]
        if any(schema.tables[r].kind == "singleton" for r in roots):
            summary[domain] = 1
        else:
            summary[domain] = sum(conn.execute(f"SELECT COUNT(*) FROM preset.{r}").fetchone()[0] for r in roots)
    return summary


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
        summary = _merge(conn, included, replace)
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
