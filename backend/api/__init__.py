"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from ..database import DB_PATH, close_wal_anchor, init_db, open_wal_anchor
from ..database.migrations import run_pending, stamp_all
from ..features.presets import schema_safety_problems as preset_schema_safety_problems
from ..inference.local_models.llama_server import manager
from .deps import FRONTEND_DIR
from .routes import ROUTERS
from .routes.storage import VACUUM_FREE_BYTES, free_bytes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A fresh install gets the latest schema + seeds straight from init_db, so
    # the migration chain has nothing to do — stamp it instead of running ~50
    # guarded no-op migrations on first boot. Checked before init_db, which
    # creates the file. (Zero-size covers a file touched but never written.)
    fresh = not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0
    migrated = False
    if fresh:
        await init_db()
        stamp_all(DB_PATH)
    else:
        # Existing tables still have their old column shape. Run migrations
        # before feeding them the latest CREATE TABLES script: CREATE TABLE IF
        # NOT EXISTS does not add columns, and a latest-schema index may name a
        # column only the pending migration knows how to add.
        migrated = bool(run_pending(DB_PATH))
        await init_db()
    # A rebuild-style migration (0027's drop/rename, 0028's DROP COLUMN /
    # DROP TABLE) leaves the old table's pages on the freelist, and the live
    # DB runs auto_vacuum=NONE, so nothing returns them: the file stays
    # bloated by the rebuilt tables' size (~25 -> ~39 MiB) until the next
    # restore happens to VACUUM. restore_full already reclaims on its private
    # copy (see presets.restore_full); this is the same reclaim for the
    # normal startup-migration path.
    #
    # The second arm covers dead space that arrives without a migration --
    # deleted conversations, cleaned-up Agent logs, a cleanup whose own
    # VACUUM lost its race with a live reader. Without it those pages are
    # stranded until a migration happens to come along. Both arms are gated so
    # an ordinary boot never rewrites the whole file. Safe here: we're before
    # `yield`, so no request connection is open to contend with the VACUUM.
    if not fresh and (migrated or free_bytes(DB_PATH) > VACUUM_FREE_BYTES):
        vac = sqlite3.connect(DB_PATH, isolation_level=None)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()
    # Schema safety check for the preset/backup engine. Non-fatal at startup: it
    # guards backup integrity, not normal queries, so a developer schema change that
    # left the live schema uncovered or unlike a fresh install must warn loudly
    # (naming the constant/migration to fix) rather than block boot. The preset ops
    # themselves still call assert_schema_safe and fail hard on the same problems.
    conn = sqlite3.connect(DB_PATH)
    try:
        problems = preset_schema_safety_problems(conn)
    finally:
        conn.close()
    if problems:
        logger.error(
            "Preset/backup schema safety check failed; exports, snapshots and restores "
            "will be refused until this is fixed:\n  - " + "\n  - ".join(problems)
        )
    logger.info("Database initialized")
    # One idle connection held for the life of the process, so the transient
    # per-query connections are never the last WAL connection and stop paying
    # 32 KiB of wal-index teardown each (see open_wal_anchor). Opened last:
    # migrations, init_db and the VACUUM above all want the file to themselves,
    # and the anchor is only useful once requests start.
    await open_wal_anchor()
    try:
        yield
    finally:
        try:
            # Every supervised llama-server child. These are Orb's only managed
            # subprocesses, and without this teardown an orphan keeps its model
            # resident and holds the GPU after Orb exits. A process that never
            # imported a feature that owns one has an empty registry and nothing
            # to do.
            await manager.shutdown_all()
        finally:
            # Nested so a child that refuses to die still releases the anchor,
            # whose close is what performs the final WAL checkpoint.
            await close_wal_anchor()


def build_app() -> FastAPI:
    """Construct and return the configured FastAPI application."""
    app = FastAPI(title="Orb", lifespan=lifespan)

    @app.middleware("http")
    async def no_cache_middleware(request: Request, call_next):
        response = await call_next(request)
        # Default to no-store for dynamic API/SSE responses, but let a handler opt
        # into caching by setting its own Cache-Control first (e.g. avatars, which
        # are large and rarely change — see api_get_avatar). setdefault preserves
        # the handler's value instead of clobbering it.
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for router in ROUTERS:
        app.include_router(router)

    # Mount static files last so concrete routes match before this catch-all.
    if os.path.isdir(FRONTEND_DIR):
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    return app
