"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..database import close_wal_anchor, current_db_path, init_db, open_wal_anchor
from ..features import slop_suggestions
from ..features.presets import schema_safety_problems as preset_schema_safety_problems
from ..inference.local_models import onnx_runtime
from ..inference.local_models.llama_server import manager
from .admission import DatasetAdmissionMiddleware
from .cache_control import CacheControlMiddleware
from .compression import TextGZipMiddleware
from .deps import FRONTEND_DIR
from .errors import register_error_handlers
from .routes import ROUTERS
from .routes.storage import VACUUM_FREE_BYTES, free_bytes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrated = await init_db()
    db_path = current_db_path()
    # Reclaim pages from rebuild migrations or accumulated deletions. Gate VACUUM
    # to avoid rewriting on ordinary boots; before yield, no request holds a connection.
    if migrated or free_bytes(db_path) > VACUUM_FREE_BYTES:
        vac = sqlite3.connect(db_path, isolation_level=None)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()
    # Schema safety check for the preset/backup engine. Non-fatal at startup: it guards backup integrity, not normal queries, so
    # a developer schema change that left the live schema uncovered or unlike a fresh install must warn loudly (naming the
    # constant/migration to fix) rather than block boot. The preset ops themselves still call assert_schema_safe and fail hard
    # on the same problems.
    conn = sqlite3.connect(db_path)
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
    # One idle connection held for the life of the process, so the transient per-query connections are never the last WAL
    # connection and stop paying 32 KiB of wal-index teardown each (see open_wal_anchor). Opened last: migrations, init_db and
    # the VACUUM above all want the file to themselves, and the anchor is only useful once requests start.
    await open_wal_anchor()
    # The Phrase Bank's suggestion miner checks for staleness once the server has
    # settled. A run happens in a child process, so it never competes with a turn.
    suggestion_check = asyncio.create_task(slop_suggestions.refresh_after_startup())
    try:
        yield
    finally:
        suggestion_check.cancel()
        try:
            await slop_suggestions.shutdown()
            # Every supervised llama-server child. Without this teardown an orphan keeps its model resident and holds the GPU
            # after Orb exits. A process that never imported a workflow or feature that owns one has an empty registry and
            # nothing to do.
            await manager.shutdown_all()
            # ONNX sessions are not subprocesses, but a 385 MB graph held past shutdown is the same class of leak as an orphaned
            # child, and the release is what lets a model file be replaced on the next start.
            onnx_runtime.release()
        finally:
            # Nested so a child that refuses to die still releases the anchor,
            # whose close is what performs the final WAL checkpoint.
            await close_wal_anchor()


def build_app() -> FastAPI:
    """Construct and return the configured FastAPI application."""
    app = FastAPI(title="Orb", lifespan=lifespan)

    # Level 6 rather than Starlette's 9: on a 900 KB conversation list, 9 costs ~20% more CPU for a body about 1% smaller.
    app.add_middleware(TextGZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(CacheControlMiddleware)
    app.add_middleware(DatasetAdmissionMiddleware)

    for router in ROUTERS:
        app.include_router(router)

    register_error_handlers(app)

    # Mount static files last so concrete routes match before this catch-all.
    if os.path.isdir(FRONTEND_DIR):
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    return app
