"""Export, import, restore, and maintain preset database files."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping

from ...database import SEEDED_PARTS, seed_library_statements
from ...database import preset_schema as ps
from ...database.connection import checkpoint_wal, current_db_path
from ...database.migrations import MIGRATIONS, run_pending
from ...database.schema import CREATE_TABLES_SQL

META_TABLE = "orb_preset_meta"

# User-facing domains, derived from the declared roots, and the parts of each profile.
ALL_DOMAINS: list[str] = sorted(set(ps.DOMAIN_ROOTS.values()))
PROFILE_PARTS: tuple[str, ...] = ps.PROFILE_PARTS

# Maximum wait for the live DB write lock during a full restore.
RESTORE_LOCK_TIMEOUT = 15.0


def _roots_for(domain: str) -> list[str]:
    """The root tables belonging to ``domain`` (reverse of ps.DOMAIN_ROOTS)."""
    return [r for r, d in ps.DOMAIN_ROOTS.items() if d == domain]


class PresetError(Exception):
    """Raised for caller-facing preset failures (bad file, version skew, etc.)."""


@dataclasses.dataclass
class Selection:
    """What a file holds, or an export takes: the global domains, and the parts of each profile by its id."""

    domains: set[str]
    profiles: dict[str, set[str]]

    def is_empty(self) -> bool:
        return not self.domains and not self.profiles


def selection(domains: Iterable[str], profiles: Mapping[str, Iterable[str]] | None, profile_ids: Iterable[str]) -> Selection:
    """Validate a selection and close it over the domains its picks need.

    Without *profiles* it is read the way a file made before profiles is: each legacy domain also stands for its part of every
    profile in *profile_ids*, and the legacy domains ``fragments`` and ``phrase_bank`` are parts only. A profile with no parts
    is left out.
    """
    wanted = set(domains)
    if profiles is None:
        legacy = {ps.LEGACY_DOMAIN_PARTS[d] for d in wanted if d in ps.LEGACY_DOMAIN_PARTS}
        picked = {pid: set(legacy) for pid in profile_ids} if legacy else {}
        wanted -= set(ps.LEGACY_DOMAIN_PARTS) - set(ALL_DOMAINS)
    else:
        picked = {str(pid): set(parts) for pid, parts in profiles.items()}
    unknown = wanted - set(ALL_DOMAINS)
    if unknown:
        raise PresetError(f"Unknown domains: {sorted(unknown)}")
    bad_parts = {part for parts in picked.values() for part in parts} - set(PROFILE_PARTS)
    if bad_parts:
        raise PresetError(f"Unknown profile parts: {sorted(bad_parts)}")
    picked = {pid: parts for pid, parts in picked.items() if parts}
    for parts in picked.values():
        for part in parts:
            wanted |= ps.PART_DOMAINS.get(part, frozenset())
    for trigger, implied in ps.IMPLIED_DOMAINS.items():
        if trigger in wanted:
            wanted |= implied  # e.g. chats are meaningless without their character
    return Selection(wanted, picked)


#
# Everything the merge engine needs -- table classification, the FK graph, a safe insert order, which edges to defer -- is read
# from the live database with PRAGMA. Adding a table or an FK column therefore requires no edit here: the model simply grows.
# The only hand-declared inputs are the product/security policy in backend/database/preset_schema.py.


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

    def part_of(self, table: str) -> str | None:
        """The profile part *table* belongs to: its PROFILE_SCOPED part, or "setup" for the profile row and what it owns."""
        if table in ps.PROFILE_SCOPED:
            return ps.PROFILE_SCOPED[table]
        return "setup" if self.root_of(table).name == ps.PROFILE_TABLE else None

    def part_tables(self) -> list[str]:
        """Every table holding profile rows, the profile table itself excluded, in topological order."""
        return [t for t in self.order if t != ps.PROFILE_TABLE and self.part_of(t) is not None]

    def domain_tables(self, domain: str) -> list[str]:
        """Tables belonging to *domain*, in topological (parent-first) order."""
        return [t for t in self.order if self.domain_of(t) == domain]


_SINGLETON_RE = re.compile(r"check\s*\(\s*id\s*=\s*1\s*\)", re.IGNORECASE)


def _build_schema_model(conn: sqlite3.Connection) -> _Schema:
    """Classify live schema from PRAGMA and DDL.

    Singleton CHECK(id=1) rows update in place; surrogate INTEGER ids remap on
    insert; stable primary keys upsert with portable identity.
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
            # to_col may be None (implicit reference to the parent's PK); it is
            # resolved in a second pass below, once every table's PK is known.
            fks.append(_FK(name, from_col, parent, to_col, on_delete, notnull.get(from_col, False)))

        if _SINGLETON_RE.search(ddl[name]):
            kind = "singleton"
        elif len(pk) == 1 and types.get(pk[0], "").startswith("INTEGER"):
            kind = "surrogate"
        else:
            kind = "stable"

        # A profile partition column is not ownership: the table stays a root, or a child of its other owner.
        owners = [
            f for f in fks if f.kind == "ownership" and not (name in ps.PROFILE_SCOPED and f.from_col == ps.PROFILE_COLUMN)
        ]
        owner = next((f for f in owners if f.from_col == ps.OWNERSHIP_COLUMNS.get(name)), None) or next(iter(owners), None)
        tables[name] = _Table(name, cols, pk, kind, fks, owner)

    # Second pass: resolve implicit FK targets now that every PK is known, so the fallback never depends on sqlite_master order
    # (a child read before its parent used to silently get "id" instead of the parent's real PK).
    for t in tables.values():
        for f in t.fks:
            if f.to_col is None:
                f.to_col = tables[f.parent].pk[0] if f.parent in tables else "id"

    order, deferred = _topo_order(tables)
    return _Schema(tables, order, deferred)


def _topo_order(tables: dict[str, _Table]) -> tuple[list[str], set[tuple[str, str]]]:
    """Topologically order tables, deferring self-links and cyclic crossrefs.

    Never defer ownership edges. Insert deferred columns as NULL, then fix them once every id map exists.
    """
    deferred: set[tuple[str, str]] = set()
    for t in tables.values():
        for f in t.fks:
            if f.is_self:
                deferred.add((t.name, f.from_col))

    # Iterate tables in a fixed (alphabetical) order, never sqlite_master's physical order. Both the emitted insert order and
    # the cycle-break choice are then a pure function of the schema's *shape*, independent of the order tables were created in
    # -- so a table rebuilt by a migration (which moves it to the end of sqlite_master) yields the identical model to a fresh
    # install. The schema-equivalence gate relies on this determinism.
    names = sorted(tables)
    placed: set[str] = set()
    order: list[str] = []
    while len(placed) < len(tables):
        progressed = False
        for name in names:
            if name in placed:
                continue
            t = tables[name]
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
        # Stalled: a cycle remains. Break it by deferring one crossref edge whose parent is still unplaced.
        broke = False
        for name in names:
            if name in placed:
                continue
            for f in tables[name].fks:
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
    """Return preset-policy coverage gaps, or [] when fully covered.

    Check table domains, FK parents and undeclared sensitive columns.
    """
    schema = _build_schema_model(conn)
    problems: list[str] = []
    for name, t in schema.tables.items():
        if schema.domain_of(name) is None and schema.part_of(name) is None:
            problems.append(
                f"table {name!r} reaches no DOMAIN_ROOT via ownership; assign its root "
                f"a domain in DOMAIN_ROOTS, a part in PROFILE_SCOPED, or add {name!r} to EXCLUDED_TABLES"
            )
        if ps.PROFILE_COLUMN in t.cols and name not in ps.PROFILE_SCOPED:
            problems.append(f"table {name!r} has a {ps.PROFILE_COLUMN} column but no part in PROFILE_SCOPED")
        for fk in t.fks:
            if fk.parent not in schema.tables:
                problems.append(
                    f"{name}.{fk.from_col} references unclassified parent {fk.parent!r} (excluded or unknown table)"
                )
        for col in t.cols:
            if (
                ps.is_sensitive_column(col)
                and (name, col) not in ps.SECRET_COLUMNS
                and (name, col) not in ps.NON_SECRET_KEY_COLUMNS
            ):
                problems.append(
                    f"column {name}.{col} looks secret but is not in SECRET_COLUMNS; add it (with its scrub value) or rename it"
                )
    # Every deferred edge is inserted NULL and fixed up afterwards (FK checks are off during the merge, but a NOT NULL
    # constraint still fires on insert). A future NOT NULL self-FK, or a NOT NULL crossref caught inside a broken cycle, would
    # therefore raise IntegrityError on every merge -- surface it here.
    for table, col in schema.deferred:
        fk = schema.tables[table].fk(col)
        if fk is not None and fk.notnull:
            problems.append(
                f"{table}.{col} is a deferred FK edge (inserted NULL, fixed up after) "
                f"but is declared NOT NULL; a merge would fail its constraint. Make it nullable."
            )

    # Reverse direction: every hand-declared policy entry must still match the live schema. A stale entry (column dropped, table
    # renamed) would otherwise surface only as a raw OperationalError mid-export, or be silently ignored.
    known_domains = set(ps.DOMAIN_ROOTS.values())
    for root in ps.DOMAIN_ROOTS:
        if root not in schema.tables:
            problems.append(f"DOMAIN_ROOTS key {root!r} is not an existing non-excluded table; fix the name or drop the entry")
            continue
        owner = schema.tables[root].owner_fk
        if owner is not None:
            problems.append(
                f"DOMAIN_ROOTS key {root!r} is not a true root -- it is owned by "
                f"{owner.parent!r} via {owner.from_col} (ON DELETE CASCADE); only roots may "
                f"map to a domain, children inherit their root's"
            )
    for table, col in ps.SECRET_COLUMNS:
        if table not in schema.tables or col not in schema.tables[table].cols:
            problems.append(f"SECRET_COLUMNS entry ({table!r}, {col!r}) does not exist in the schema; drop it or fix the name")
    for table, cols in ps.PRESERVED_COLUMNS.items():
        for col in cols:
            if table not in schema.tables or col not in schema.tables[table].cols:
                problems.append(
                    f"PRESERVED_COLUMNS entry ({table!r}, {col!r}) does not exist in the schema; drop it or fix the name"
                )
    for table in ps.PROFILE_SCOPED:
        if table not in schema.tables or ps.PROFILE_COLUMN not in schema.tables[table].cols:
            problems.append(f"PROFILE_SCOPED entry {table!r} is not a table with a {ps.PROFILE_COLUMN} column")
    if ps.PROFILE_TABLE not in schema.tables:
        problems.append(f"PROFILE_TABLE {ps.PROFILE_TABLE!r} is not an existing non-excluded table")
    for part in [*ps.PROFILE_SCOPED.values(), *ps.PART_DOMAINS]:
        if part not in ps.PROFILE_PARTS:
            problems.append(f"profile part {part!r} is not in PROFILE_PARTS")
    for trigger, implied in ps.IMPLIED_DOMAINS.items():
        if trigger not in known_domains:
            problems.append(f"IMPLIED_DOMAINS trigger {trigger!r} is not a known domain")
        for dom in implied:
            if dom not in known_domains:
                problems.append(f"IMPLIED_DOMAINS implied domain {dom!r} (for trigger {trigger!r}) is not a known domain")
    return problems


def _edge_set(t: _Table) -> set[tuple]:
    """A table's FK edges as comparable tuples (order-independent)."""
    return {(f.from_col, f.parent, f.to_col, f.on_delete, f.notnull) for f in t.fks}


def schema_equivalence_problems(conn: sqlite3.Connection) -> list[str]:
    """Compare live schema with a fresh install; return divergence reasons.

    Check columns, primary keys, classification, FK edges and deferred edges so upgraded databases use the same merge semantics.
    """
    live = _build_schema_model(conn)
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(CREATE_TABLES_SQL)
        canon = _build_schema_model(ref)
    finally:
        ref.close()

    problems: list[str] = []
    live_names, canon_names = set(live.tables), set(canon.tables)
    for name in sorted(canon_names - live_names):
        problems.append(f"table {name!r} is in the canonical schema but missing from the live DB")
    for name in sorted(live_names - canon_names):
        problems.append(f"table {name!r} is in the live DB but not the canonical schema (CREATE_TABLES_SQL)")

    for name in sorted(live_names & canon_names):
        lt, ct = live.tables[name], canon.tables[name]
        # Compare column *sets*, not ordered lists: the merge engine names every column explicitly (never relies on position),
        # and an ALTER-added column legitimately lands at a different ordinal on an old install than on a fresh one. A missing
        # or extra column, by contrast, is a real merge hazard.
        missing = set(ct.cols) - set(lt.cols)
        extra = set(lt.cols) - set(ct.cols)
        if missing:
            problems.append(f"{name}: live is missing column(s) {sorted(missing)} present in the canonical schema")
        if extra:
            problems.append(
                f"{name}: live has extra column(s) {sorted(extra)} absent from the canonical schema "
                f"(a stale column a migration added but never dropped -> write a cleanup migration)"
            )
        if lt.pk != ct.pk:
            problems.append(f"{name}: primary key differs -- live {lt.pk} vs canonical {ct.pk}")
        if lt.kind != ct.kind:
            problems.append(f"{name}: merge kind differs -- live {lt.kind!r} vs canonical {ct.kind!r}")
        live_edges, canon_edges = _edge_set(lt), _edge_set(ct)
        for from_col, parent, to_col, on_delete, _nn in sorted(canon_edges - live_edges):
            problems.append(
                f"{name}.{from_col}: live has no matching FK, canonical has "
                f"{parent}({to_col}) ON DELETE {on_delete} -> write a rebuild migration"
            )
        for from_col, parent, to_col, on_delete, _nn in sorted(live_edges - canon_edges):
            problems.append(
                f"{name}.{from_col}: live has FK {parent}({to_col}) ON DELETE {on_delete} absent from the canonical schema"
            )

    only_canon = canon.deferred - live.deferred
    only_live = live.deferred - canon.deferred
    if only_canon:
        problems.append(f"deferred FK edges in the canonical schema but not live: {sorted(only_canon)}")
    if only_live:
        problems.append(f"deferred FK edges in the live schema but not canonical: {sorted(only_live)}")
    return problems


def schema_safety_problems(conn: sqlite3.Connection) -> list[str]:
    """Return coverage or equivalence problems, or [] if safe.

    Startup warns on this list; preset operations reject it via assert_schema_safe.
    """
    return schema_coverage_problems(conn) + schema_equivalence_problems(conn)


def assert_schema_safe(conn: sqlite3.Connection) -> None:
    """Raise PresetError for policy gaps or fresh/migrated schema divergence.

    Gate every preset operation; startup uses non-fatal schema_safety_problems.
    """
    problems = schema_safety_problems(conn)
    if problems:
        raise PresetError("Preset schema safety check failed:\n  - " + "\n  - ".join(problems))


def _snapshots_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(current_db_path())), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def _library_path(name: str) -> str:
    """Resolve a library filename after rejecting path traversal."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.db", name):
        raise PresetError("Invalid backup name")
    root = os.path.realpath(_snapshots_dir())
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep):
        raise PresetError("Invalid backup name")
    if not os.path.isfile(path):
        raise PresetError("Backup not found")
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


def _write_meta(conn: sqlite3.Connection, sel: Selection, label: str, kind: str, keys_stripped: bool) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  included_domains TEXT NOT NULL,"
        "  created_at TEXT NOT NULL,"
        "  label TEXT NOT NULL DEFAULT '',"
        "  kind TEXT NOT NULL,"
        "  keys_stripped INTEGER NOT NULL DEFAULT 0,"
        "  profiles TEXT"
        ")"
    )
    # A meta table from a file made before profiles lacks the column; NULL there means "derive the parts from the domains".
    if "profiles" not in {r[1] for r in conn.execute(f"PRAGMA table_info({META_TABLE})")}:  # nosec B608 -- constant identifier
        conn.execute(f"ALTER TABLE {META_TABLE} ADD COLUMN profiles TEXT")  # nosec B608 -- constant identifier
    conn.execute(f"DELETE FROM {META_TABLE}")  # nosec B608 -- schema-derived identifier, values parameterised
    conn.execute(
        f"INSERT INTO {META_TABLE} (id, included_domains, created_at, label, kind, keys_stripped, profiles) "  # nosec B608 -- schema-derived identifier, values parameterised
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        (
            json.dumps(sorted(sel.domains)),
            datetime.datetime.now().isoformat(timespec="seconds"),
            label,
            kind,
            int(keys_stripped),
            json.dumps({pid: sorted(parts) for pid, parts in sel.profiles.items()}),
        ),
    )


def _full_selection(conn: sqlite3.Connection) -> Selection:
    """Every domain and every part of every profile *conn* holds."""
    ids = [str(r[0]) for r in conn.execute(f"SELECT id FROM {ps.PROFILE_TABLE}")]  # nosec B608 -- constant identifier
    return Selection(set(ALL_DOMAINS), {pid: set(PROFILE_PARTS) for pid in ids})


def _file_profiles(conn: sqlite3.Connection) -> dict[str, str]:
    """The profiles a file holds, id to name. A file older than profiles names its presets or layouts instead."""
    for table in (ps.PROFILE_TABLE, "presets", "prompt_layouts"):
        try:
            rows = conn.execute(f"SELECT id, name FROM {table} ORDER BY id != 'default', name COLLATE NOCASE").fetchall()  # nosec B608 -- constant identifiers
        except sqlite3.OperationalError:
            continue
        return {str(pid): str(name) for pid, name in rows}
    return {}


def _stamp_migrations(conn: sqlite3.Connection) -> None:
    """Mark every current migration as applied.

    A preset we build is always cloned from the live DB, whose schema is current by definition, so the preset's schema is
    current too. Stamping the full set makes that explicit and keeps ``check_and_upgrade`` a no-op on our own files
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
    """A file's meta: its global domains, and ``profiles`` as ``[{id, name, parts}]``. A file made before profiles is read as
    its domains say (see ``selection``); one whose library rows had no profile to hold them shows the placeholder 'default',
    which the upgrade creates."""
    conn = sqlite3.connect(path)
    try:
        try:
            row = conn.execute(
                f"SELECT included_domains, created_at, label, kind, keys_stripped FROM {META_TABLE} WHERE id = 1"  # nosec B608 -- schema-derived identifier, values parameterised
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        try:
            (raw,) = conn.execute(f"SELECT profiles FROM {META_TABLE} WHERE id = 1").fetchone()  # nosec B608 -- constant identifier
        except sqlite3.OperationalError:
            raw = None
        names = _file_profiles(conn)
    finally:
        conn.close()
    stored = json.loads(raw) if raw else None
    if stored is None and not names:
        names = {"default": "Default"}
    sel = selection([d for d in json.loads(row[0]) if d in ALL_DOMAINS or d in ps.LEGACY_DOMAIN_PARTS], stored, names)
    order = [*names, *(pid for pid in sel.profiles if pid not in names)]
    return {
        "included_domains": sorted(sel.domains),
        "profiles": [
            {"id": pid, "name": names.get(pid, pid), "parts": [p for p in PROFILE_PARTS if p in sel.profiles[pid]]}
            for pid in order
            if pid in sel.profiles
        ],
        "created_at": row[1],
        "label": row[2],
        "kind": row[3],
        "keys_stripped": bool(row[4]),
    }


def _meta_selection(meta: Mapping) -> Selection:
    return Selection(set(meta["included_domains"]), {p["id"]: set(p["parts"]) for p in meta["profiles"]})


def is_full(meta: Mapping | None) -> bool:
    """Whether a file with *meta* (None: a raw database) holds every domain and every part of each of its profiles, so a
    restore replaces the whole database with it."""
    if meta is None:
        return True
    return set(meta["included_domains"]) >= set(ALL_DOMAINS) and all(
        set(p["parts"]) >= set(PROFILE_PARTS) for p in meta["profiles"]
    )


def _assert_integrity(conn: sqlite3.Connection, what: str) -> None:
    """Raise ``PresetError`` unless ``PRAGMA integrity_check`` reports ``ok``.

    Run on a file we just produced (VACUUM INTO) or are about to trust (a restore target). A truncated or torn disk write yields
    a structurally broken database that opens fine but is silently corrupt; this is the trip that stops such a file from
    becoming the backup the user relies on.
    """
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise PresetError(f"Integrity check failed for {what}: {row[0] if row else 'no result'}")


# Excluded tables that may legitimately hold rows (bookkeeping, not domain data).
# Every *other* excluded table must stay empty, or its data would ship in no backup.
_EXCLUDED_MAY_HAVE_ROWS: frozenset[str] = frozenset(
    {META_TABLE, "schema_migrations", "dataset_meta", *ps.DERIVED_TABLES, *ps.LOCAL_TABLES}
)


def _scratch_connect(path: str) -> sqlite3.Connection:
    """Open a private temp copy without journal or fsync; an integrity check gates whatever it produces."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


def _drop_local_rows(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA secure_delete = ON")
    for tbl in ps.LOCAL_TABLES:
        conn.execute(f"DELETE FROM {tbl}")  # nosec B608 -- constant identifier
    conn.execute("PRAGMA secure_delete = OFF")


def _carry_local_rows(prepared: str, live: str) -> None:
    conn = _scratch_connect(prepared)
    try:
        conn.execute("ATTACH DATABASE ? AS live", (live,))
        for tbl in ps.LOCAL_TABLES:
            conn.execute(f"DELETE FROM main.{tbl}")  # nosec B608 -- constant identifier
            conn.execute(f"INSERT INTO main.{tbl} SELECT * FROM live.{tbl}")  # nosec B608 -- constant identifier
    finally:
        conn.close()


def _blank_json_leaf(node: object, path: tuple[str, ...]) -> bool:
    """Blank the leaf `path` names inside `node`. Returns whether anything changed.

    ``"*"`` matches every key at that level, which is what a provider map keyed by provider id needs: ``cloud.providers.<any
    id>.api_key``. Everything not on a declared path is left byte-identical -- an imported ComfyUI graph's node inputs sit in
    the same column and must survive untouched.
    """
    if not isinstance(node, dict) or not path:
        return False
    head, rest = path[0], path[1:]
    keys = list(node) if head == "*" else ([head] if head in node else [])
    changed = False
    for key in keys:
        if rest:
            changed = _blank_json_leaf(node[key], rest) or changed
        elif node[key] != "":
            node[key] = ""
            changed = True
    return changed


def _blank_json_paths(conn: sqlite3.Connection, table: str, column: str, paths: tuple[tuple[str, ...], ...]) -> None:
    """Scrub declared JSON secret paths row by row.

    Python traversal supports wildcard keys and non-singleton workflow_state
    columns that the singleton SECRET_COLUMNS loop cannot handle.
    """
    if not paths:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return
    rows = conn.execute(f"SELECT rowid, {column} FROM {table}").fetchall()  # nosec B608 -- schema-derived identifier
    for rowid, raw in rows:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        changed = False
        for path in paths:
            # Not `any(...)`: every declared path must be walked, and a generator would stop at the first one that matched.
            changed = _blank_json_leaf(payload, path) or changed
        if changed:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE rowid = ?",  # nosec B608 -- schema-derived identifier, values parameterised
                (json.dumps(payload), rowid),
            )


def _scrub_configs(conn: sqlite3.Connection, schema: _Schema) -> None:
    """Remove personal configuration and secrets from an export."""
    for root, domain in ps.DOMAIN_ROOTS.items():
        if domain == "configs" and schema.tables[root].kind != "singleton":
            conn.execute(f"DELETE FROM {root}")  # nosec B608 -- schema-derived identifier, values parameterised
    for (table, col), blank in ps.SECRET_COLUMNS.items():
        if schema.tables[table].kind == "singleton":
            conn.execute(f"UPDATE {table} SET {col} = ?", (blank,))  # nosec B608 -- schema-derived identifier, values parameterised
    for (table, col), paths in ps.SECRET_JSON_PATHS.items():
        _blank_json_paths(conn, table, col, paths)


def _profile_clause(schema: _Schema, table: str, ids_sql: str, db: str = "main") -> str:
    """A WHERE clause selecting *table*'s rows that belong to the profiles *ids_sql* names (a subquery or placeholders); an
    owner more than one hop away is looked up in database *db*."""
    if table == ps.PROFILE_TABLE:
        return f"{schema.tables[table].pk[0]} IN ({ids_sql})"
    if table in ps.PROFILE_SCOPED:
        return f"{ps.PROFILE_COLUMN} IN ({ids_sql})"
    fk = schema.tables[table].owner_fk
    assert fk is not None
    if fk.parent == ps.PROFILE_TABLE:
        return f"{fk.from_col} IN ({ids_sql})"
    inner = _profile_clause(schema, fk.parent, ids_sql, db)
    return f"{fk.from_col} IN (SELECT {fk.to_col} FROM {db}.{fk.parent} WHERE {inner})"


def _drop_part(conn: sqlite3.Connection, schema: _Schema, profile_id: str, part: str) -> None:
    """Delete one part of a profile; without its setup the profile row keeps only its anchor columns."""
    for table in reversed(schema.part_tables()):
        if schema.part_of(table) == part:
            conn.execute(f"DELETE FROM {table} WHERE {_profile_clause(schema, table, '?')}", (profile_id,))  # nosec B608 -- schema-derived identifiers
    if part == "setup":
        info = conn.execute(f"PRAGMA table_info({ps.PROFILE_TABLE})").fetchall()
        blanks = [f"{r[1]} = {r[4] if r[4] is not None else 'NULL'}" for r in info if r[1] not in ps.PROFILE_ANCHOR_COLUMNS]
        conn.execute(f"UPDATE {ps.PROFILE_TABLE} SET {', '.join(blanks)} WHERE id = ?", (profile_id,))  # nosec B608 -- schema-derived identifiers and defaults


def _prune_profiles(conn: sqlite3.Connection, schema: _Schema, profiles: Mapping[str, set[str]]) -> None:
    """Keep only *profiles*, each with only its parts; works with foreign keys on or off."""
    ids = list(profiles)
    keep = ",".join("?" * len(ids))
    for table in [*reversed(schema.part_tables()), ps.PROFILE_TABLE]:
        where = f" WHERE NOT ({_profile_clause(schema, table, keep)})" if ids else ""
        conn.execute(f"DELETE FROM {table}{where}", ids)  # nosec B608 -- schema-derived identifiers
    for profile_id, parts in profiles.items():
        for part in PROFILE_PARTS:
            if part not in parts:
                _drop_part(conn, schema, profile_id, part)


def _rename_profile(conn: sqlite3.Connection, schema: _Schema, old: str, new: str) -> None:
    """Give profile *old* the id *new* in a working copy, with foreign keys off. The rows it owns (its prompt blocks) get fresh
    ids, so they never take over the rows of the profile they were copied from: an id starting ``'<old>:'`` (a marker block)
    starts ``'<new>:'``, any other a new uuid."""
    for table in schema.part_tables():
        if table in ps.PROFILE_SCOPED:
            conn.execute(f"UPDATE {table} SET {ps.PROFILE_COLUMN} = ? WHERE {ps.PROFILE_COLUMN} = ?", (new, old))  # nosec B608 -- schema-derived identifiers
            continue
        t = schema.tables[table]
        fk = t.owner_fk
        if fk is None or fk.parent != ps.PROFILE_TABLE or t.kind != "stable" or len(t.pk) != 1:
            raise PresetError(f"{table} cannot move to another profile")
        pk = t.pk[0]
        for (row_id,) in conn.execute(f"SELECT {pk} FROM {table} WHERE {fk.from_col} = ?", (old,)).fetchall():  # nosec B608 -- schema-derived identifiers
            fresh = f"{new}:{row_id[len(old) + 1 :]}" if str(row_id).startswith(f"{old}:") else uuid.uuid4().hex
            conn.execute(f"UPDATE {table} SET {pk} = ?, {fk.from_col} = ? WHERE {pk} = ?", (fresh, new, row_id))  # nosec B608 -- schema-derived identifiers
    conn.execute(f"UPDATE {ps.PROFILE_TABLE} SET id = ? WHERE id = ?", (new, old))  # nosec B608 -- constant identifier


def _clone_domains(conn: sqlite3.Connection, live: str, sel: Selection) -> None:
    """Copy the live schema, the selected domains' rows and the selected profiles' rows; dangling keys then take their ON
    DELETE action, as a delete would. The caller prunes the profiles' unselected parts."""
    conn.execute("ATTACH DATABASE ? AS live", (live,))
    conn.execute(f"PRAGMA main.page_size = {int(conn.execute('PRAGMA live.page_size').fetchone()[0])}")
    ddl = conn.execute("SELECT type, name, sql FROM live.sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    tables = [(name, sql) for kind, name, sql in ddl if kind == "table" and not name.startswith("sqlite_")]
    conn.execute("BEGIN")  # one read transaction on live: every table comes from the same snapshot
    for _name, sql in tables:
        conn.execute(sql)
    schema = _build_schema_model(conn)
    skipped = set(ps.DERIVED_TABLES) | set(ps.LOCAL_TABLES)
    ids = list(sel.profiles)
    for name, _sql in tables:
        if name in skipped:
            continue
        where, params = "", []
        if name in schema.tables and schema.tables[name].kind != "singleton":
            domain, part = schema.domain_of(name), schema.part_of(name)
            if (domain is None and part is None) or (domain is not None and domain not in sel.domains):
                continue
            if part is not None:
                if not ids:
                    continue
                where = f" WHERE {_profile_clause(schema, name, ','.join('?' * len(ids)), 'live')}"
                params = ids
        conn.execute(f"INSERT INTO main.{name} SELECT * FROM live.{name}{where}", params)  # nosec B608 -- schema-derived identifier
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        conn.execute("DELETE FROM main.sqlite_sequence")
        conn.execute("INSERT INTO main.sqlite_sequence SELECT * FROM live.sqlite_sequence")
    # Indexes and triggers come after the rows, so no insert trigger fires on copied data.
    for kind, name, sql in ddl:
        if kind != "table" and not name.startswith("sqlite_"):
            conn.execute(sql)
    conn.execute("COMMIT")
    conn.execute("DETACH DATABASE live")
    conn.execute("PRAGMA foreign_keys=ON")
    seen: list = []
    while dangling := conn.execute("PRAGMA foreign_key_check").fetchall():
        if dangling == seen:
            raise PresetError(f"Could not prune {len(dangling)} rows left pointing at excluded data; aborted.")
        seen = dangling
        groups: dict[tuple[str, int], list[int]] = {}
        for table, rowid, _parent, fk_id in dangling:
            groups.setdefault((table, fk_id), []).append(rowid)
        for (table, fk_id), rowids in groups.items():
            edge = [r for r in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall() if r[0] == fk_id]
            if edge[0][6] == "CASCADE":
                action = f"DELETE FROM {table}"  # nosec B608 -- schema-derived identifier
            elif edge[0][6] == "SET NULL":
                action = f"UPDATE {table} SET " + ", ".join(f"{r[3]} = NULL" for r in edge)  # nosec B608 -- schema-derived identifier
            else:
                raise PresetError(f"{table} has rows that need the left-out {edge[0][2]} rows; export that domain too.")
            for i in range(0, len(rowids), 500):
                chunk = rowids[i : i + 500]
                conn.execute(f"{action} WHERE rowid IN ({','.join('?' * len(chunk))})", chunk)


def build_preset(
    selected_domains: Iterable[str],
    strip_keys: bool,
    label: str = "",
    kind: str = "manual",
    profiles: Mapping[str, Iterable[str]] | None = None,
) -> str:
    """Clone the live DB, prune what was not selected, tag with meta, store in the library. Returns the on-disk file name.

    *profiles* maps a profile id to the parts to save for it; without it every profile takes the parts the legacy domains name
    (``configs`` its setup, ``fragments``, ``phrase_bank``, ``lorebooks`` its global marks)."""
    src = sqlite3.connect(current_db_path())
    try:
        assert_schema_safe(src)
        live_ids = [str(r[0]) for r in src.execute(f"SELECT id FROM {ps.PROFILE_TABLE}")]  # nosec B608 -- constant identifier
    finally:
        src.close()
    sel = selection(selected_domains, profiles, live_ids)
    missing = set(sel.profiles) - set(live_ids)
    if missing:
        raise PresetError(f"Unknown profiles: {sorted(missing)}")
    if sel.is_empty():
        raise PresetError("Select at least one thing to export")
    selected = sel.domains

    tmp = os.path.join(_snapshots_dir(), f".build-{os.getpid()}-{datetime.datetime.now():%H%M%S%f}.tmp")

    keys_stripped = False
    packed = f"{tmp}.packed"
    c = _scratch_connect(tmp)
    try:
        _clone_domains(c, current_db_path(), sel)
        _assert_integrity(c, "the exported backup clone")
        c.execute("PRAGMA foreign_keys=ON")
        schema = _build_schema_model(c)
        # Tripwire: an excluded table that carries data would be invisible to both export and merge -- its rows would silently
        # never be backed up. The only excluded data table is message_attachments, empty by invariant post-0020; this fails
        # loudly the day someone parks a live table in EXCLUDED_TABLES.
        for tbl in ps.EXCLUDED_TABLES:
            if tbl in _EXCLUDED_MAY_HAVE_ROWS:
                continue
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                continue
            if c.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone():  # nosec B608 -- schema-derived identifier, values parameterised
                raise PresetError(
                    f"Excluded table {tbl!r} has rows but is invisible to export and merge; "
                    f"its data would silently never be backed up. Give its root a domain in "
                    f"DOMAIN_ROOTS, or confirm it must stay excluded."
                )
        for tbl in ps.DERIVED_TABLES:
            c.execute(f"DELETE FROM {tbl}")  # nosec B608 -- constant identifier
        _drop_local_rows(c)
        # Prune each unselected domain by deleting its root tables: with FK on, a CASCADE prunes the owned children and a SET
        # NULL clears soft pointers, so no per-child delete is hand-coded. configs is special (it scrubs the singleton in place
        # rather than deleting it).
        for domain in ALL_DOMAINS:
            if domain in selected:
                continue
            if domain == "configs":
                _scrub_configs(c, schema)
                continue
            for root in _roots_for(domain):
                if schema.tables[root].kind != "singleton":
                    c.execute(f"DELETE FROM {root}")  # nosec B608 -- schema-derived identifier, values parameterised
        _prune_profiles(c, schema, sel.profiles)
        if "configs" in selected and strip_keys:
            for table, col in ((t, col) for (t, col) in ps.SECRET_COLUMNS if col in ps.CREDENTIAL_LEAVES):
                c.execute(f"UPDATE {table} SET {col} = ''")  # nosec B608 -- schema-derived identifier, values parameterised
            # The same rule, one level deeper: "every declared secret whose leaf is a credential", not "every column literally
            # named api_key". A key inside a JSON column is the same key.
            for (table, col), paths in ps.SECRET_JSON_PATHS.items():
                _blank_json_paths(c, table, col, tuple(p for p in paths if p and p[-1] in ps.CREDENTIAL_LEAVES))
            keys_stripped = True
        _stamp_migrations(c)
        _write_meta(c, sel, label, kind, keys_stripped)
        # Repack so freed pages, stripped keys included, leave the file; in-place VACUUM costs several times VACUUM INTO.
        c.execute("VACUUM INTO ?", (packed,))
        c.close()
        chk = sqlite3.connect(packed)
        try:
            _assert_integrity(chk, "the exported backup")
        finally:
            chk.close()
        name = _unique_name(kind, label)
        shutil.move(packed, os.path.join(_snapshots_dir(), name))
    finally:
        c.close()
        for p in (tmp, packed):
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.remove(p)
    return name


# Generic merge: clear replaced domains/subtrees, insert in dependency order with surrogate-id remapping, fix deferred links,
# then reconcile soft pointers. New schema children and FK columns follow these passes automatically.


def _existing(conn: sqlite3.Connection, cache: dict[str, set], parent: str, to_col: str) -> set:
    """Memoised set of a parent table's current key values in ``main``.

    Used for the "keep this value -- it still resolves locally" branch of the FK rewrite. A parent is always fully
    inserted/upserted before any child consults it (topological order), and parents are never re-touched afterwards, so the set
    is stable once built.
    """
    if parent not in cache:
        cache[parent] = {r[0] for r in conn.execute(f"SELECT {to_col} FROM main.{parent}")}  # nosec B608 -- schema-derived identifier, values parameterised
    return cache[parent]


def _resolve_fk(value, fk: _FK, idmaps: dict[str, dict[int, int]], conn, cache) -> tuple[object, bool]:
    """Translate a foreign key; return (new_value, drop).

    None stays None. Surrogate parents resolve only through this merge's id map; stable/untouched parents must exist in main.
    Dangling nullable/SET NULL keys become NULL; dangling required ownership keys drop the child row.
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

    Walks the ownership chain up from ``table`` to ``root``, building nested subqueries: the final hop targets ``preset.root``
    (the entities being re-imported), the intermediate hops join through ``main``. This generalises the hand-written "delete
    this conversation's message tree" prune.
    """
    fk = schema.tables[table].owner_fk
    assert fk is not None
    if fk.parent == root:
        return f"{fk.from_col} IN (SELECT {fk.to_col} FROM preset.{root})"
    inner = _scope_clause(schema, fk.parent, root)
    return f"{fk.from_col} IN (SELECT {fk.to_col} FROM main.{fk.parent} WHERE {inner})"


def _merge_table(conn, schema, table, idmaps, cache, insert_only: frozenset = frozenset()) -> None:
    """Insert/upsert one covered table, rewriting FKs and recording its id-map. A stable row whose key is in *insert_only* is inserted
    only when it is missing, never overwriting the live one."""
    t = schema.tables[table]
    cols = t.cols
    deferred = {c for (tbl, c) in schema.deferred if tbl == table}
    fks = {f.from_col: f for f in t.fks}

    if t.kind == "singleton":
        # Update the lone row in place; never insert/delete it. PRESERVED_COLUMNS
        # keep their local values (cache bookkeeping, not config from the file).
        pk = t.pk[0]
        keep = set(t.pk) | set(ps.PRESERVED_COLUMNS.get(table, ()))
        row = conn.execute(f"SELECT {','.join(cols)} FROM preset.{table} WHERE {pk} = 1").fetchone()  # nosec B608 -- schema-derived identifier, values parameterised
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
        conn.execute(f"UPDATE main.{table} SET {', '.join(sets)} WHERE {pk} = 1", vals)  # nosec B608 -- schema-derived identifier, values parameterised
        return

    if t.kind == "stable":
        # Identity is portable: upsert by primary key (the child-replace in phase B already cleared any subtree this row owns).
        ph = ",".join("?" * len(cols))
        key = [cols.index(c) for c in t.pk]
        for row in conn.execute(f"SELECT {','.join(cols)} FROM preset.{table}").fetchall():  # nosec B608 -- schema-derived identifier, values parameterised
            vals = list(row)
            for i, c in enumerate(cols):
                if c in deferred:
                    vals[i] = None  # fixed up once the referenced rows exist
                elif c in fks:
                    vals[i], _ = _resolve_fk(vals[i], fks[c], idmaps, conn, cache)
            verb = "IGNORE" if tuple(row[i] for i in key) in insert_only else "REPLACE"
            conn.execute(f"INSERT OR {verb} INTO main.{table} ({','.join(cols)}) VALUES ({ph})", vals)
        return

    # surrogate: reinsert dropping the autoincrement id, record old->new.
    (pk,) = t.pk
    ins_cols = [c for c in cols if c != pk]
    ph = ",".join("?" * len(ins_cols))
    idmap: dict[int, int] = {}
    for row in conn.execute(f"SELECT {','.join(cols)} FROM preset.{table}").fetchall():  # nosec B608 -- schema-derived identifier, values parameterised
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
        new = conn.execute(f"INSERT INTO main.{table} ({','.join(ins_cols)}) VALUES ({ph})", vals).lastrowid  # nosec B608 -- schema-derived identifier, values parameterised
        assert new is not None
        idmap[rowd[pk]] = new
    idmaps[table] = idmap


def _break_self_cycles(pointer: dict) -> None:
    """Null the closing pointer of each self-FK cycle in place so imported trees terminate."""
    for start in pointer:
        seen: set = set()
        cur = start
        while cur is not None and cur in pointer:
            if cur in seen:
                pointer[cur] = None  # break the cycle here -> this node becomes a root
                break
            seen.add(cur)
            cur = pointer[cur]


def _fixup_deferred(conn, schema, table, from_col, idmaps, cache) -> None:
    """Resolve deferred FK values after all id maps exist.

    Translate original values to new identities; break self-link cycles before writing.
    """
    t = schema.tables[table]
    fk = t.fk(from_col)
    assert fk is not None
    pk = t.pk[0]
    own_map = idmaps.get(table)  # surrogate tables only
    resolved: dict = {}  # new_pk -> new_val, in the post-merge id space
    for row in conn.execute(f"SELECT {pk}, {from_col} FROM preset.{table}").fetchall():  # nosec B608 -- schema-derived identifier, values parameterised
        old_pk, old_val = row[0], row[1]
        if own_map is not None and old_pk not in own_map:
            continue  # row was dropped during insert
        new_pk = own_map[old_pk] if own_map is not None else old_pk
        new_val, _ = _resolve_fk(old_val, fk, idmaps, conn, cache)
        resolved[new_pk] = new_val
    if fk.is_self:
        _break_self_cycles(resolved)
    for new_pk, new_val in resolved.items():
        conn.execute(f"UPDATE main.{table} SET {from_col} = ? WHERE {pk} = ?", (new_val, new_pk))  # nosec B608 -- schema-derived identifier, values parameterised


def _reconcile_crossref(conn, fk: _FK, idmaps, remap: bool) -> None:
    """Reconcile soft pointers after parent rows are remapped."""
    table, col = fk.table, fk.from_col
    pmap = idmaps.get(fk.parent)
    if remap and pmap:
        conn.execute("CREATE TEMP TABLE _fk_remap (old INTEGER PRIMARY KEY, new INTEGER)")
        conn.executemany("INSERT INTO _fk_remap (old, new) VALUES (?, ?)", list(pmap.items()))
        conn.execute(
            f"UPDATE main.{table} SET {col} = (SELECT new FROM _fk_remap WHERE old = {col}) "  # nosec B608 -- schema-derived identifier, values parameterised
            f"WHERE {col} IN (SELECT old FROM _fk_remap)"
        )
        conn.execute("DROP TABLE _fk_remap")
    conn.execute(
        f"UPDATE main.{table} SET {col} = NULL "  # nosec B608 -- schema-derived identifier, values parameterised
        f"WHERE {col} IS NOT NULL AND {col} NOT IN (SELECT {fk.to_col} FROM main.{fk.parent})"
    )


def _merge(
    conn: sqlite3.Connection, included: set[str], replace: bool, profiles: Mapping[str, set[str]] | None = None
) -> dict[str, int]:
    """Merge the attached ``preset`` file's *included* domains, and the parts of each of *profiles* (its ids as the file holds
    them), into ``main``. A profile's rows replace only that profile's rows of the same part; other profiles are untouched."""
    schema = _build_schema_model(conn)
    profiles = dict(profiles or {})
    parts_in = {part for parts in profiles.values() for part in parts}
    conn.execute("CREATE TEMP TABLE _profile_parts (profile_id TEXT NOT NULL, part TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO temp._profile_parts (profile_id, part) VALUES (?, ?)",
        [(pid, part) for pid, parts in profiles.items() for part in parts],
    )

    def merged(table: str) -> bool:
        if table == ps.PROFILE_TABLE:
            return bool(profiles)
        domain, part = schema.domain_of(table), schema.part_of(table)
        if domain is None and part is None:
            return False
        return (domain is None or domain in included) and (part is None or part in parts_in)

    def scope(table: str) -> str | None:
        """The rows of ``main.table`` this file's profiles replace; None for a table not split by profile."""
        part = schema.part_of(table)
        if part is None or table == ps.PROFILE_TABLE:
            return None
        return _profile_clause(schema, table, f"SELECT profile_id FROM temp._profile_parts WHERE part = '{part}'")

    inc = [t for t in schema.order if merged(t)]
    fully_replaced: set[str] = set()

    # A. Restore only: empty each additive domain or profile part (one whose entity roots are stable-key tables that apply
    #    merges by upsert) so it ends up matching the file exactly. Those with no stable root -- configs (singleton + replaced
    #    surrogate roots), a profile's phrase bank -- are already full replacements on apply, so they are left to phase B. A
    #    table split by profile loses only the file's profiles' rows, and the rows whose owner does not come back.
    if replace:
        for domain in included:
            roots = _roots_for(domain)
            if roots and all(schema.tables[r].kind == "stable" for r in roots):
                for table in reversed(schema.domain_tables(domain)):
                    where = scope(table)
                    if where is not None:
                        where = f"({where}) OR NOT ({_scope_clause(schema, table, schema.root_of(table).name)})"
                    conn.execute(f"DELETE FROM main.{table}" + (f" WHERE {where}" if where else ""))  # nosec B608 -- schema-derived identifier, values parameterised
                    fully_replaced.add(table)
        for part in parts_in:
            tables = [t for t in schema.part_tables() if schema.part_of(t) == part and schema.domain_of(t) is None]
            roots = [t for t in tables if schema.tables[t].owner_fk is None]
            if roots and all(schema.tables[r].kind == "stable" for r in roots):
                for table in reversed(tables):
                    conn.execute(f"DELETE FROM main.{table} WHERE {scope(table)}")  # nosec B608 -- schema-derived identifier
                    fully_replaced.add(table)

    # B. Child-replace: clear the subtree each incoming entity supersedes, child
    #    first. A table whose entity root is stable is replaced per-root (scoped to
    #    the incoming ids); the stable root itself is left for the upsert. A table
    #    whose root is surrogate (endpoints/model_configs, user_personas) has no
    #    portable identity, so it is wiped wholesale; a profile's phrase bank is
    #    wiped for the file's profiles only.
    for table in reversed(inc):
        t = schema.tables[table]
        if t.kind == "singleton" or table in fully_replaced:
            continue
        root = schema.root_of(table)
        where = scope(table)
        if root.kind == "stable":
            if table != root.name:
                owned = _scope_clause(schema, table, root.name)
                conn.execute(f"DELETE FROM main.{table} WHERE {owned}" + (f" AND ({where})" if where else ""))  # nosec B608 -- schema-derived identifier, values parameterised
        elif where is not None:
            conn.execute(f"DELETE FROM main.{table} WHERE {where}")  # nosec B608 -- schema-derived identifier
        else:
            conn.execute(f"DELETE FROM main.{table}")  # nosec B608 -- schema-derived identifier, values parameterised
            fully_replaced.add(table)

    # C. Insert/upsert in topological order so every parent precedes its children. A profile the file holds without its setup
    #    is only an anchor for its other parts: it is added when missing, and never overwrites the live profile.
    cache: dict[str, set] = {}
    idmaps: dict[str, dict[int, int]] = {}
    anchors = frozenset((pid,) for pid, parts in profiles.items() if "setup" not in parts)
    for table in inc:
        _merge_table(conn, schema, table, idmaps, cache, anchors if table == ps.PROFILE_TABLE else frozenset())

    # D. Fix up deferred self/cycle back-pointers now that every id-map exists.
    for table, col in schema.deferred:
        if merged(table):
            _fixup_deferred(conn, schema, table, col, idmaps, cache)

    # E. Reconcile soft pointers into any fully-replaced parent: cross-domain ones, and same-domain SET NULL ones in rows the
    #    merge kept (a live preset the file lacks still names endpoints phase B removed, so it loses them as a delete would).
    for t in schema.tables.values():
        for fk in t.fks:
            if (
                fk.kind == "crossref"
                and (t.name, fk.from_col) not in schema.deferred
                and fk.parent in fully_replaced
                and (
                    schema.domain_of(t.name) != schema.domain_of(fk.parent)
                    or (fk.on_delete == "SET NULL" and t.name not in fully_replaced)
                )
            ):
                # Rows of a merged child domain were already FK-rewritten in phase
                # C; only remap the parent map for child tables left untouched.
                _reconcile_crossref(conn, fk, idmaps, remap=not merged(t.name))
    conn.execute("DROP TABLE temp._profile_parts")

    # Row counts per merged domain (configs, anchored on its singleton, reports 1), and the profiles merged.
    summary: dict[str, int] = {"profiles": len(profiles)} if profiles else {}
    for domain in included:
        roots = _roots_for(domain)
        if any(schema.tables[r].kind == "singleton" for r in roots):
            summary[domain] = 1
        else:
            summary[domain] = sum(conn.execute(f"SELECT COUNT(*) FROM preset.{r}").fetchone()[0] for r in roots)  # nosec B608 -- schema-derived identifier, values parameterised
    return summary


# How a file's profile lands: on the profile with the same id, or on a new one.
TARGETS: tuple[str, ...] = ("same", "new")


def _live_profile_ids() -> set[str]:
    conn = sqlite3.connect(current_db_path())
    try:
        return {str(r[0]) for r in conn.execute(f"SELECT id FROM {ps.PROFILE_TABLE}")}  # nosec B608 -- constant identifier
    finally:
        conn.close()


def _plan_targets(
    held: Mapping[str, set[str]], choices: Mapping[str, Mapping] | None, live_ids: set[str]
) -> dict[str, tuple[str, set[str]]]:
    """Each profile a file holds, as (the live profile id it lands on, the parts to take). *choices* maps a file profile id to
    ``{"target": "same" | "new", "parts": [...]}``; a profile it does not name takes every part it holds, onto the same profile
    when that id exists here and onto a new one otherwise. A profile with no parts taken is left out."""
    choices = choices or {}
    unknown = set(choices) - set(held)
    if unknown:
        raise PresetError(f"The file holds no profiles {sorted(unknown)}")
    plan: dict[str, tuple[str, set[str]]] = {}
    for pid, parts in held.items():
        choice = choices.get(pid, {})
        target = choice.get("target") or ("same" if pid in live_ids else "new")
        if target not in TARGETS:
            raise PresetError(f"Unknown target: {target!r}")
        taken = set(parts) if choice.get("parts") is None else set(choice["parts"]) & set(parts)
        if taken:
            plan[pid] = (pid if target == "same" else uuid.uuid4().hex, taken)
    return plan


def _retarget(work: str, plan: Mapping[str, tuple[str, set[str]]]) -> dict[str, set[str]]:
    """Prune a working copy to the planned parts and move each profile onto its target id; returns the parts by target."""
    conn = _scratch_connect(work)
    try:
        schema = _build_schema_model(conn)
        conn.execute("BEGIN")
        _prune_profiles(conn, schema, {pid: parts for pid, (_target, parts) in plan.items()})
        for pid, (target, _parts) in plan.items():
            if target != pid:
                _rename_profile(conn, schema, pid, target)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {target: parts for target, parts in plan.values()}


def apply_preset(preset_path: str, *, replace: bool = False, choices: Mapping[str, Mapping] | None = None) -> dict:
    """Merge a preset into the live database by identity; *choices* says where each of its profiles lands (``_plan_targets``).
    A profile the merge creates gets the seed fragments and phrase bank for the parts it did not take, as a new profile does."""
    work = os.path.join(_snapshots_dir(), f".apply-{os.getpid()}-{datetime.datetime.now():%H%M%S%f}.tmp")
    shutil.copyfile(preset_path, work)
    try:
        check_and_upgrade(work)  # quick_check + validate + migrate, all on the copy
        held = file_selection(work)
        included = held.domains
        live_ids = _live_profile_ids()
        profiles = _retarget(work, _plan_targets(held.profiles, choices, live_ids))
        created = {target: set(SEEDED_PARTS) - parts for target, parts in profiles.items() if target not in live_ids}

        conn = sqlite3.connect(current_db_path(), isolation_level=None)
        summary: dict[str, int] = {}
        try:
            assert_schema_safe(conn)
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("ATTACH DATABASE ? AS preset", (work,))
            conn.execute("BEGIN")
            summary = _merge(conn, included, replace, profiles)
            for target, missing in created.items():
                for sql, params in seed_library_statements(target, missing):
                    conn.execute(sql, params)
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                conn.execute("ROLLBACK")
                raise PresetError(f"Import would corrupt foreign keys ({len(problems)} violations); aborted.")
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise
        finally:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("DETACH DATABASE preset")
            conn.close()
        # A replacing merge (restore_partial) rewrites whole domains in one
        # transaction -- the same database-sized WAL write as a full restore.
        checkpoint_wal(current_db_path())
        return summary
    finally:
        for sfx in ("", "-wal", "-shm"):
            p = work + sfx
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.remove(p)


def restore_partial(preset_path: str, choices: Mapping[str, Mapping] | None = None) -> dict:
    """Roll the covered domains and profile parts back to a partial file (scoped restore).

    Replaces each domain and each profile part the file carries to match it exactly and leaves the rest untouched. The
    full-coverage counterpart is ``restore_full``.
    """
    return apply_preset(preset_path, replace=True, choices=choices)


def _backup_scope(preset_path: str, choices: Mapping[str, Mapping] | None = None) -> Selection | None:
    """What a merge of ``preset_path`` can write (its own domains plus any pointing into them, and the parts of the live
    profiles it lands on), or None when only a full backup can undo it: a surrogate-keyed table comes back from a partial
    restore under new ids."""
    meta = read_meta(preset_path)
    held = _meta_selection(meta) if meta else None
    live_ids = _live_profile_ids()
    if held is None:
        return None
    plan = _plan_targets(held.profiles, choices, live_ids)
    profiles = {target: parts for target, parts in plan.values() if target in live_ids}
    included = held.domains
    conn = sqlite3.connect(current_db_path())
    try:
        schema = _build_schema_model(conn)
    finally:
        conn.close()
    scope = set(included)
    for t in schema.tables.values():
        domain = schema.domain_of(t.name)
        if domain and any(fk.kind == "crossref" and schema.domain_of(fk.parent) in included for fk in t.fks):
            scope.add(domain)
    for trigger, implied in ps.IMPLIED_DOMAINS.items():
        if trigger in scope:
            scope |= implied
    if (not scope and not profiles) or scope >= set(ALL_DOMAINS):
        return None
    tables = [t for domain in scope for t in schema.domain_tables(domain)]
    tables += [t for t in schema.part_tables() if any(schema.part_of(t) in parts for parts in profiles.values())]
    if any(schema.tables[t].kind == "surrogate" for t in tables):
        return None
    return Selection(scope, profiles)


def create_snapshot(label: str = "", merging: str | None = None, choices: Mapping[str, Mapping] | None = None) -> str:
    """Back up the live DB (or, before merging a partial file, what that merge can write) into the pruned library."""
    scope = _backup_scope(merging, choices) if merging else None
    if scope:
        name = build_preset(scope.domains, False, label, kind="auto", profiles=scope.profiles)
        prune_auto()
        return name
    name = _unique_name("auto", label)
    dest = os.path.join(_snapshots_dir(), name)
    src = sqlite3.connect(current_db_path())
    try:
        assert_schema_safe(src)
        src.execute("VACUUM INTO ?", (dest,))
    finally:
        src.close()
    c = sqlite3.connect(dest, isolation_level=None)
    try:
        _assert_integrity(c, "the snapshot")
        _drop_local_rows(c)
        _stamp_migrations(c)
        _write_meta(c, _full_selection(c), label, "auto", False)
    finally:
        c.close()
    prune_auto()
    return name


def snapshot_profile(profile_id: str, label: str = "") -> str:
    """Back up one profile on its own: its setup, fragments and phrase bank. Its global marks need the Worlds they point at,
    so they stay out rather than pulling in every lorebook."""
    name = build_preset([], False, label, kind="auto", profiles={profile_id: ["setup", "fragments", "phrase_bank"]})
    prune_auto()
    return name


def _align_page_size(prepared: str, live: str) -> None:
    """Match the prepared database's page size to the live destination.

    SQLite online backup cannot change page size while the destination is in WAL
    mode; mismatches otherwise fail with a misleading SQLITE_READONLY error.
    """
    probe = sqlite3.connect(live)
    try:
        want = int(probe.execute("PRAGMA page_size").fetchone()[0])
    finally:
        probe.close()
    conn = sqlite3.connect(prepared, isolation_level=None)
    try:
        if int(conn.execute("PRAGMA page_size").fetchone()[0]) == want:
            return
        conn.execute(f"PRAGMA page_size={want}")  # PRAGMA values cannot be bound; want is an int read from the live DB
        conn.execute("VACUUM")  # a page-size change only takes effect on rebuild
    finally:
        conn.close()


def _copy_over_live(prepared: str, live: str) -> None:
    """Atomically copy a prepared database over the live one with online backup.

    Incomplete copies roll back. A raising progress callback bounds BUSY retries
    that Connection.backup would otherwise repeat indefinitely.
    """
    deadline = time.monotonic() + RESTORE_LOCK_TIMEOUT

    def _bound_wait(status: int, remaining: int, total: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() > deadline:
            raise PresetError(
                f"Restore could not get a write lock on the database within {RESTORE_LOCK_TIMEOUT:.0f}s "
                "-- another operation still holds it. Nothing was changed; try again."
            )

    src = sqlite3.connect(prepared)
    try:
        dest = sqlite3.connect(live, timeout=RESTORE_LOCK_TIMEOUT)
        try:
            src.backup(dest, progress=_bound_wait)
        finally:
            dest.close()
    finally:
        src.close()


def restore_full(name: str) -> None:
    """Replace the live database with a full preset."""
    src = _library_path(name)
    live = current_db_path()
    tmp = f"{live}.restore-{os.getpid()}"
    shutil.copyfile(src, tmp)
    try:
        # Drop the preset marker so it doesn't ride along in the live DB, and bring the file up to the current schema -- all on
        # the temp copy, which nothing else has open.
        conn = _scratch_connect(tmp)
        try:
            conn.execute(f"DROP TABLE IF EXISTS {META_TABLE}")
        finally:
            conn.close()
        if run_pending(tmp):
            # A rebuild-style migration (0027's drop/rename, 0028's column drops) leaves the old table's pages on the freelist,
            # so restoring a pre-rebuild snapshot bloated the live DB by the rebuilt tables' size (~24 -> ~35 MiB). Reclaim it
            # here, on the still-private copy, *before* the integrity check so the check validates the exact bytes that get
            # swapped in. Skipped when no migration ran: library files are VACUUM INTO products, already compact.
            vac = sqlite3.connect(tmp, isolation_level=None)
            try:
                vac.execute("VACUUM")
            finally:
                vac.close()
        _carry_local_rows(tmp, live)
        # The page size has to match before the copy, and rewriting it rebuilds the file -- so do it ahead of the checks below,
        # which must validate the exact bytes that land in the live DB.
        _align_page_size(tmp, live)
        # The temp copy's contents are about to become the live DB; refuse a
        # structurally broken or FK-inconsistent file rather than copying it in.
        chk = sqlite3.connect(tmp)
        try:
            _assert_integrity(chk, "the restore target")
            fk = chk.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise PresetError(f"Restore target has {len(fk)} foreign-key violations; aborted.")
        finally:
            chk.close()
        _copy_over_live(tmp, live)
        # The backup copies every page of the prepared file into the live database as one transaction, so all of it lands in the
        # WAL. With the lifespan anchor open no last-close checkpoint follows, and the WAL would keep the whole database's size;
        # reclaim it here instead.
        checkpoint_wal(live)
    finally:
        # Unlike the old rename, the copy leaves the temp file behind on success
        # as well as on failure -- plus any journal our own connections created.
        for sfx in ("", "-wal", "-shm"):
            p = tmp + sfx
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.remove(p)


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
                "profiles": meta.get("profiles", []),
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
    autos = sorted((e for e in list_library() if e["kind"] == "auto"), key=lambda x: x["mtime"], reverse=True)
    for entry in autos[keep:]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(_snapshots_dir(), entry["name"]))


def check_and_upgrade(path: str) -> None:
    """Validate a file is an Orb database and migrate it up to the current
    schema. Rejects files produced by a newer Orb build."""
    conn = sqlite3.connect(path)
    try:
        # quick_check on the upload before we trust it enough to migrate: a torn or tampered file that still opens must be
        # rejected, not run through run_pending (which would write into a corrupt database).
        qc = conn.execute("PRAGMA quick_check").fetchone()
        if not qc or qc[0] != "ok":
            raise PresetError(f"Uploaded file failed its integrity check: {qc[0] if qc else 'no result'}")
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
            "This backup was made with a newer version of Orb "
            f"(unknown migrations: {sorted(unknown)}). Update Orb to import it."
        )
    run_pending(path)


def file_selection(path: str) -> Selection:
    """What a file declares, or everything for a raw (meta-less) Orb DB."""
    meta = read_meta(path)
    if meta is not None:
        return _meta_selection(meta)
    conn = sqlite3.connect(path)
    try:
        return _full_selection(conn)
    finally:
        conn.close()


def ingest_upload(tmp_path: str, label: str) -> str:
    """Validate, upgrade and store an uploaded database; return its filename.

    Always tag it imported while retaining its declared domain coverage.
    """
    check_and_upgrade(tmp_path)
    existing = read_meta(tmp_path)
    keys_stripped = existing["keys_stripped"] if existing else False
    label = existing["label"] if existing and existing["label"] else label
    c = sqlite3.connect(tmp_path, isolation_level=None)
    try:
        # Read after the upgrade, a file made before profiles now names the profiles that hold its library.
        _write_meta(c, _meta_selection(existing) if existing else _full_selection(c), label, "imported", keys_stripped)
    finally:
        c.close()
    name = _unique_name("imported", label)
    dest = os.path.join(_snapshots_dir(), name)
    shutil.move(tmp_path, dest)
    return name
