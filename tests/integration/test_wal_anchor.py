"""The lifespan-scoped WAL anchor: one idle connection held open for the life of
the process so the transient per-query connections are never the last WAL
connection.

Why this exists at all is in ``connection.open_wal_anchor``'s docstring. What
these tests defend is the *shape* of the anchor rather than the byte count: it
must follow the patched ``DB_PATH``, never leak across lifespans, and above all
stay completely idle -- no statement, no cursor, no transaction -- because
``VACUUM`` and the restore path's online backup both fail against a connection
holding a lock.

Deliberately no assertion on OS write bytes. That measurement is a
macOS-``proc_pid_rusage`` manual benchmark, not something portable CI can see.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI

import backend.api as api_module
import backend.database.connection as db_connection

_TS = "2024-01-01T00:00:00"


@pytest.fixture(autouse=True)
async def _no_anchor_leak():
    """No test may hand the next one a live anchor.

    The anchor is process-global module state, and a test that leaves one open
    on its own temp path would hold that file (and a worker thread) for the
    rest of the session.
    """
    yield
    try:
        await db_connection.close_wal_anchor()
    except Exception:
        # A test that deliberately makes close raise has already had its module
        # state cleared -- close_wal_anchor clears before it closes.
        pass


class _ExplodingConnection:
    """Connects, then fails the setup PRAGMAs. Stands in for a file that opens
    but cannot be put into WAL mode (a read-only volume, say)."""

    def __init__(self) -> None:
        self.closed = False
        self.row_factory = None

    def execute(self, sql, parameters=None):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    async def close(self) -> None:
        self.closed = True


# ── lifecycle ────────────────────────────────────────────────────────────


async def test_open_is_idempotent_for_the_same_path(db_path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))

    await db_connection.open_wal_anchor()
    first = db_connection._wal_anchor
    await db_connection.open_wal_anchor()

    assert first is not None
    assert db_connection._wal_anchor is first  # not a second connection
    assert db_connection._wal_anchor_path == str(db_path)


async def test_close_is_idempotent_and_clears_state(db_path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))

    await db_connection.open_wal_anchor()
    anchor = db_connection._wal_anchor
    await db_connection.close_wal_anchor()
    await db_connection.close_wal_anchor()  # no-op, must not raise

    assert db_connection._wal_anchor is None
    assert db_connection._wal_anchor_path is None
    assert anchor is not None
    with pytest.raises(ValueError):
        await anchor.execute("SELECT 1")


async def test_anchor_uses_the_currently_patched_db_path(db_path, monkeypatch):
    """Resolved at call time, not import time -- every test in the suite
    monkeypatches ``connection.DB_PATH`` after this module is imported."""
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await db_connection.open_wal_anchor()

    async with db_connection.get_db() as db:
        await db.execute("CREATE TABLE anchor_probe (v TEXT)")
        await db.execute("INSERT INTO anchor_probe (v) VALUES ('seen')")
        await db.commit()

    anchor = db_connection._wal_anchor
    assert anchor is not None
    async with anchor.execute("SELECT v FROM anchor_probe") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "seen"


async def test_anchoring_a_different_path_closes_the_previous_anchor(tmp_path, monkeypatch):
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"

    monkeypatch.setattr(db_connection, "DB_PATH", str(first_path))
    await db_connection.open_wal_anchor()
    first = db_connection._wal_anchor

    monkeypatch.setattr(db_connection, "DB_PATH", str(second_path))
    await db_connection.open_wal_anchor()

    assert db_connection._wal_anchor is not first
    assert db_connection._wal_anchor_path == str(second_path)
    assert first is not None
    with pytest.raises(ValueError):
        await first.execute("SELECT 1")  # the old one is closed, not leaked


async def test_a_failed_open_leaves_no_module_state(tmp_path, monkeypatch):
    """A directory where the database file should be: opens nothing."""
    blocked = tmp_path / "not-a-file.db"
    blocked.mkdir()
    monkeypatch.setattr(db_connection, "DB_PATH", str(blocked))

    with pytest.raises(sqlite3.OperationalError):
        await db_connection.open_wal_anchor()

    assert db_connection._wal_anchor is None
    assert db_connection._wal_anchor_path is None


async def test_a_failed_pragma_closes_the_half_open_connection(db_path, monkeypatch):
    """A half-open anchor would hold the file without the WAL mode the rest of
    the process assumes, so the connection is closed before the error escapes."""
    fake = _ExplodingConnection()

    async def _connect(_path):
        return fake

    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    monkeypatch.setattr(db_connection.aiosqlite, "connect", _connect)

    with pytest.raises(sqlite3.OperationalError):
        await db_connection.open_wal_anchor()

    assert fake.closed
    assert db_connection._wal_anchor is None
    assert db_connection._wal_anchor_path is None


async def test_close_clears_state_even_when_close_raises():
    """Otherwise the next ``open_wal_anchor`` would see a dead connection object
    and take the idempotent early return, leaving the process unanchored."""

    class _StuckConnection:
        async def close(self):
            raise sqlite3.OperationalError("close failed")

    db_connection._wal_anchor = _StuckConnection()  # type: ignore[assignment]
    db_connection._wal_anchor_path = "/nowhere/app.db"

    with pytest.raises(sqlite3.OperationalError):
        await db_connection.close_wal_anchor()

    assert db_connection._wal_anchor is None
    assert db_connection._wal_anchor_path is None


# ── the mechanism itself ─────────────────────────────────────────────────


async def test_anchor_keeps_the_wal_alive_across_transient_connections(db_path, monkeypatch):
    """The whole point, in the one form portable CI can see.

    The measured saving is OS write bytes, which only the macOS ``rusage`` probe
    can read. What causes it is visible anywhere: SQLite's last WAL connection
    to close checkpoints and *deletes* the WAL and its shared-memory wal-index,
    and the next connection recreates them. The anchor is what makes a transient
    connection stop being the last one.
    """
    wal = Path(f"{db_path}-wal")
    shm = Path(f"{db_path}-shm")
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))

    async with db_connection.get_db() as db:
        async with db.execute("SELECT 1") as cur:
            await cur.fetchall()
        assert shm.exists()  # created on open ...
    assert not wal.exists() and not shm.exists()  # ... and torn down on close

    await db_connection.open_wal_anchor()
    async with db_connection.get_db() as db:
        async with db.execute("SELECT 1") as cur:
            await cur.fetchall()
    assert wal.exists() and shm.exists()  # the teardown that no longer happens

    await db_connection.close_wal_anchor()
    assert not wal.exists() and not shm.exists()  # last one out still cleans up


# ── transient connection compatibility ───────────────────────────────────


async def test_transient_reads_and_writes_work_with_the_anchor_open(db_path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await db_connection.open_wal_anchor()

    async with db_connection.get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id, title, created_at) VALUES ('anchored', 'Anchored', ?)",
            (_TS,),
        )
        await db.commit()

    async with db_connection.get_db() as db:
        async with db.execute("SELECT title FROM conversations WHERE id = 'anchored'") as cur:
            row = await cur.fetchone()
    assert row is not None and row["title"] == "Anchored"


async def test_concurrent_readers_and_a_serialized_writer(db_path, monkeypatch):
    """WAL's reader/writer concurrency is the reason Orb runs WAL at all; the
    anchor must not change it. Two overlapping readers plus the ``BEGIN
    IMMEDIATE`` writer path, all through transient connections."""
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await db_connection.open_wal_anchor()

    async def read() -> int:
        async with db_connection.get_db() as db:
            async with db.execute("SELECT COUNT(*) AS n FROM settings") as cur:
                row = await cur.fetchone()
        assert row is not None
        return int(row["n"])

    async def write(cid: str) -> None:
        async with db_connection.immediate_tx() as db:
            await db.execute("INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)", (cid, cid, _TS))

    a, b = await asyncio.gather(read(), read())
    assert a == b
    await asyncio.gather(write("w-1"), write("w-2"))

    async with db_connection.get_db() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM conversations WHERE id LIKE 'w-%'") as cur:
            row = await cur.fetchone()
    assert row is not None and row["n"] == 2


async def test_integrity_survives_anchor_close_and_reopen(db_path, monkeypatch):
    """Closing the anchor is what performs the final WAL checkpoint, so this is
    the shutdown path's correctness check."""
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))

    await db_connection.open_wal_anchor()
    async with db_connection.get_db() as db:
        await db.execute("INSERT INTO conversations (id, title, created_at) VALUES ('cycle', 'Cycle', ?)", (_TS,))
        await db.commit()
    await db_connection.close_wal_anchor()
    await db_connection.open_wal_anchor()

    anchor = db_connection._wal_anchor
    assert anchor is not None
    async with anchor.execute("PRAGMA integrity_check") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "ok"
    async with anchor.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "wal"


# ── FastAPI lifespan ─────────────────────────────────────────────────────


async def test_lifespan_opens_the_anchor_after_database_initialization(tmp_path, monkeypatch):
    """Migrations, ``init_db`` and the startup VACUUM all want the file to
    themselves; the anchor is only opened once they are done."""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    monkeypatch.setattr(api_module, "DB_PATH", str(path))

    seen: dict[str, set[str]] = {}
    real_open = db_connection.open_wal_anchor

    async def _spy() -> None:
        conn = sqlite3.connect(str(path))
        try:
            seen["tables"] = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        await real_open()

    monkeypatch.setattr(api_module, "open_wal_anchor", _spy)

    async with api_module.lifespan(FastAPI()):
        assert db_connection._wal_anchor is not None
        assert db_connection._wal_anchor_path == str(path)

    assert "settings" in seen["tables"]  # init_db ran
    assert "schema_migrations" in seen["tables"]  # stamp_all ran


async def test_lifespan_closes_the_anchor_on_a_normal_exit(db_path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    monkeypatch.setattr(api_module, "DB_PATH", str(db_path))

    async with api_module.lifespan(FastAPI()):
        anchor = db_connection._wal_anchor
        assert anchor is not None

    assert db_connection._wal_anchor is None
    assert db_connection._wal_anchor_path is None
    with pytest.raises(ValueError):
        await anchor.execute("SELECT 1")


async def test_lifespan_closes_the_anchor_when_child_shutdown_raises(db_path, monkeypatch):
    """A llama-server child that refuses to die must not cost the final WAL
    checkpoint -- hence the nested ``finally``."""
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    monkeypatch.setattr(api_module, "DB_PATH", str(db_path))

    async def _boom() -> None:
        raise RuntimeError("child refused to stop")

    monkeypatch.setattr(api_module.manager, "shutdown_all", _boom)

    with pytest.raises(RuntimeError, match="child refused to stop"):
        async with api_module.lifespan(FastAPI()):
            assert db_connection._wal_anchor is not None

    assert db_connection._wal_anchor is None
    assert db_connection._wal_anchor_path is None


async def test_repeated_lifespans_on_different_paths_do_not_leak(tmp_path, _fresh_db_template, monkeypatch):
    """The suite runs many lifespans against many temp databases in one
    process. An anchor surviving into the next one would pin a deleted file."""
    import shutil

    anchors = []
    for name in ("one.db", "two.db"):
        path = tmp_path / name
        shutil.copyfile(_fresh_db_template, path)
        monkeypatch.setattr(db_connection, "DB_PATH", str(path))
        monkeypatch.setattr(api_module, "DB_PATH", str(path))
        async with api_module.lifespan(FastAPI()):
            assert db_connection._wal_anchor_path == str(path)
            anchors.append(db_connection._wal_anchor)
        assert db_connection._wal_anchor is None

    assert anchors[0] is not anchors[1]
    for anchor in anchors:
        with pytest.raises(ValueError):
            await anchor.execute("SELECT 1")


# ── maintenance operations ───────────────────────────────────────────────


async def test_vacuum_succeeds_with_the_idle_anchor(db_path, monkeypatch):
    """``VACUUM`` fails against an open transaction or a lock that prevents
    writes. The anchor holds neither, and the storage-cleanup route must keep
    reclaiming pages with it open."""
    from backend.api.routes.storage import free_bytes, vacuum_sync

    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await db_connection.open_wal_anchor()

    async with db_connection.get_db() as db:
        await db.execute("CREATE TABLE bulk (id INTEGER PRIMARY KEY, blob TEXT)")
        await db.executemany("INSERT INTO bulk (blob) VALUES (?)", [("x" * 4000,) for _ in range(500)])
        await db.execute("DELETE FROM bulk")
        await db.commit()
    stranded = free_bytes(str(db_path))
    assert stranded > 0

    assert vacuum_sync(str(db_path)) is True
    assert free_bytes(str(db_path)) < stranded

    anchor = db_connection._wal_anchor
    assert anchor is not None
    async with anchor.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "wal"


async def test_full_restore_succeeds_with_the_idle_anchor(client, db_path):
    """``restore_full`` copies over the live database through SQLite's online
    backup, which takes a write transaction on the live file. The anchor must
    neither block it nor keep serving pre-restore content afterwards."""
    from backend.features.presets import engine as presets

    from .test_presets import _full_snapshot

    await client.post("/api/characters", json={"name": "Before"})
    snap = await _full_snapshot(client, "anchored")
    await client.post("/api/characters", json={"name": "After"})

    await db_connection.open_wal_anchor()
    anchor = db_connection._wal_anchor
    assert anchor is not None

    presets.restore_full(snap)  # must not raise: the anchor holds no lock

    async with anchor.execute("SELECT name FROM character_cards ORDER BY name") as cur:
        names = {row[0] for row in await cur.fetchall()}
    assert names == {"Before"}  # the anchor sees the restored content

    async with anchor.execute("PRAGMA integrity_check") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "ok"
    async with anchor.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "wal"

    names = {c["name"] for c in (await client.get("/api/characters")).json()}
    assert names == {"Before"}


# ── whole-database maintenance must not strand a database-sized WAL ──────


async def _bulk_up(db, mib: int = 10) -> None:
    """Grow the database by roughly *mib* MiB of character-card rows, so a
    whole-file rewrite is unmistakable next to the WAL that carries it."""
    blob = "x" * 50_000
    await db.executemany(
        "INSERT INTO character_cards (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        [(f"bulk-{i}", blob, _TS, _TS) for i in range(mib * 21)],
    )
    await db.commit()


async def test_vacuum_does_not_strand_a_database_sized_wal(db_path, db, monkeypatch):
    """``VACUUM`` rewrites every page through the WAL. Before the anchor, the
    VACUUM connection was the last one out and SQLite's own last-close
    checkpoint deleted the WAL with it -- the anchor removes that close, so the
    reclaim has to be explicit or the WAL simply inherits the file's size."""
    from backend.api.routes.storage import vacuum_sync

    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await _bulk_up(db)
    await db.execute("DELETE FROM character_cards WHERE name LIKE 'bulk-%'")
    await db.commit()

    await db_connection.open_wal_anchor()
    assert vacuum_sync(str(db_path)) is True

    wal = Path(f"{db_path}-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    assert wal_bytes < 1024 * 1024, f"WAL left at {wal_bytes:,} B after VACUUM"


async def test_full_restore_does_not_strand_a_database_sized_wal(client, db_path, db):
    """The restore's online backup writes the whole prepared file into the live
    database as one transaction, so every page of it lands in the WAL."""
    from backend.features.presets import engine as presets

    from .test_presets import _full_snapshot

    await _bulk_up(db)
    snap = await _full_snapshot(client, "bulky")

    await db_connection.open_wal_anchor()
    presets.restore_full(snap)

    wal = Path(f"{db_path}-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    db_bytes = Path(db_path).stat().st_size
    assert db_bytes > 8 * 1024 * 1024, "the fixture did not actually get big"
    assert wal_bytes < 1024 * 1024, f"WAL left at {wal_bytes:,} B beside a {db_bytes:,} B database"


async def test_replacing_preset_merge_does_not_strand_a_large_wal(client, db_path, db):
    """``restore_partial`` replaces whole domains in one transaction, which is
    the same database-sized write in a different shape."""
    from backend.features.presets import engine as presets

    from .test_presets import _full_snapshot, _snap_dir

    await _bulk_up(db)
    snap = await _full_snapshot(client, "partial")

    await db_connection.open_wal_anchor()
    presets.restore_partial(str(_snap_dir(db_path) / snap))

    wal = Path(f"{db_path}-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    assert wal_bytes < 1024 * 1024, f"WAL left at {wal_bytes:,} B after a replacing merge"


# ── concurrent opens ─────────────────────────────────────────────────────


async def test_concurrent_opens_create_exactly_one_connection(db_path, monkeypatch):
    """Two overlapping opens must not both get past the ``is None`` check.

    The first one awaits ``aiosqlite.connect`` and yields the loop right there,
    which is the window a second caller used to walk into: both connected, the
    second overwrote the global, and the first was left open with nothing
    referencing it -- unreachable, and missed by ``close_wal_anchor``.
    """
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    opened = []
    real_connect = db_connection.aiosqlite.connect

    def _counting_connect(path, *a, **kw):
        conn = real_connect(path, *a, **kw)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db_connection.aiosqlite, "connect", _counting_connect)

    await asyncio.gather(db_connection.open_wal_anchor(), db_connection.open_wal_anchor())

    assert len(opened) == 1, f"{len(opened)} connections opened for one anchor"
    assert db_connection._wal_anchor is opened[0]

    await db_connection.close_wal_anchor()
    for conn in opened:
        with pytest.raises(ValueError):
            await conn.execute("SELECT 1")  # nothing left alive
