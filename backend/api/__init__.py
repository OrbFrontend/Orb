"""HTTP API layer: the FastAPI app factory.

``build_app()`` assembles the application from its parts:
  - the startup ``lifespan`` (DB init, pending migrations + VACUUM, schema
    safety check),
  - the ``no_cache_middleware``,
  - every domain router under ``api/routes/`` (see ``routes.ROUTERS``), and
  - the static ``frontend/`` mount, attached **last** so concrete routes match
    before the ``/static`` catch-all.

``backend.main`` calls this and exposes the result as ``app`` so the
Dockerfiles' ``uvicorn backend.main:app`` and the integration conftest's
``from backend.main import app`` keep working.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from ..database import DB_PATH, init_db, schema_safety_problems
from ..database.migrations import run_pending
from .deps import FRONTEND_DIR
from .routes import ROUTERS

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if run_pending(DB_PATH):
        # A rebuild-style migration (0027's drop/rename, 0028's DROP COLUMN /
        # DROP TABLE) leaves the old table's pages on the freelist, and the live
        # DB runs auto_vacuum=NONE, so nothing returns them: the file stays
        # bloated by the rebuilt tables' size (~25 -> ~39 MiB) until the next
        # restore happens to VACUUM. restore_full already reclaims on its private
        # copy (see presets.restore_full); this is the same reclaim for the
        # normal startup-migration path. Gated on run_pending's return so a
        # boot with no pending migration doesn't rewrite the whole DB. Safe
        # here: we're before `yield`, so no request connection is open to
        # contend with the VACUUM.
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
    problems = schema_safety_problems()
    if problems:
        logger.error(
            "Preset/backup schema safety check failed; exports, snapshots and restores "
            "will be refused until this is fixed:\n  - " + "\n  - ".join(problems)
        )
    logger.info("Database initialized")
    yield


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
