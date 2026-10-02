"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..database import DB_PATH, close_wal_anchor, init_db, open_wal_anchor
from ..features.presets import schema_safety_problems as preset_schema_safety_problems
from ..inference.local_models import onnx_runtime
from ..inference.local_models.llama_server import manager
from .admission import DatasetAdmissionMiddleware
from .cache_control import CacheControlMiddleware
from .compression import TextGZipMiddleware
from .deps import FRONTEND_DIR
from .routes import ROUTERS
from .routes.storage import VACUUM_FREE_BYTES, free_bytes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrated = await init_db()
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
    if migrated or free_bytes(DB_PATH) > VACUUM_FREE_BYTES:
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
            # imported a workflow or feature that owns one has an empty registry and nothing
            # to do.
            await manager.shutdown_all()
            # ONNX sessions are not subprocesses, but a 385 MB graph held past
            # shutdown is the same class of leak as an orphaned child, and the
            # release is what lets a model file be replaced on the next start.
            onnx_runtime.release()
        finally:
            # Nested so a child that refuses to die still releases the anchor,
            # whose close is what performs the final WAL checkpoint.
            await close_wal_anchor()


def build_app() -> FastAPI:
    """Construct and return the configured FastAPI application."""
    app = FastAPI(title="Orb", lifespan=lifespan)

    # Level 6 rather than Starlette's 9: on a 900 KB conversation list, 9 costs
    # ~20% more CPU for a body about 1% smaller.
    app.add_middleware(TextGZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(CacheControlMiddleware)
    app.add_middleware(DatasetAdmissionMiddleware)

    for router in ROUTERS:
        app.include_router(router)

    # Mount static files last so concrete routes match before this catch-all.
    if os.path.isdir(FRONTEND_DIR):
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    return app
