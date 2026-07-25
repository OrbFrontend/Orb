"""Storage inspection and age-based data cleanup.

Two axes, deliberately kept orthogonal: *what* to clean (artifacts, Agent
logs) and *how old* it has to be. The GET route previews both against the same
cutoff the POST route will use, so the size shown is the size freed.

Artifacts are evicted, not deleted -- the row and its recovery metadata survive
so the image comes back through the normal rehydrate button. Agent logs are
wiped: the payload columns (raw output, reasoning, tool calls, injection block)
are blanked while the row itself stays, because the pipeline reads mood state
back off old rows. Which columns those are is the database layer's business --
see LOG_KEEP_COLUMNS -- not something the user is asked to pick through.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from ...core.locks import maintenance_lock
from ...database import DB_PATH, logs_size_before, wipe_logs_older_than
from ...workflows.attachment_cache import aged_artifact_size, evict_older_than
from ..schemas import CleanupRequest

router = APIRouter()

# Reclaiming dead pages means rewriting the whole database file, so it is only
# worth doing once enough of them have piled up. An absolute floor rather than a
# ratio: dead space only matters in absolute terms, and a ratio both spares a
# 10 GB db carrying 30 MB of free pages (right) and spares a 194 MB db carrying
# 46 MB at 23.4% (wrong -- that is the case this feature exists for).
VACUUM_FREE_BYTES = 32 * 1024 * 1024


def _cutoff(days: int) -> str | None:
    """ISO-8601 UTC cutoff for ``days`` back; None (= no age limit) for 0."""
    if days <= 0:
        return None
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def free_bytes(db_path: str = DB_PATH) -> int:
    """Bytes sitting on the SQLite freelist -- deleted data not yet returned to
    the OS. The db runs ``auto_vacuum=NONE``, so this only shrinks on VACUUM.

    Sync on purpose: two PRAGMA header reads, cheap enough that offloading them
    would cost more than it saves, and it keeps one implementation usable from
    both the request path and the (pre-``yield``, sync) startup gate.
    """
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    finally:
        conn.close()
    return pages * size


def vacuum_sync(db_path: str = DB_PATH) -> bool:
    """Rewrite the db file, returning freed pages to the OS. True if it ran.

    Best-effort by design: VACUUM needs the whole file to itself, so a reader
    that happens to be mid-query makes it SQLITE_BUSY. Losing that race is not
    an error -- the startup gate reclaims the same pages on the next boot.

    Synchronous and slow (it rewrites every page), so request-path callers must
    hand this to a thread rather than block the event loop on it.
    """
    vac = sqlite3.connect(db_path, isolation_level=None)
    try:
        vac.execute("PRAGMA busy_timeout = 5000")
        vac.execute("VACUUM")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        vac.close()


@router.get("/api/storage")
async def api_storage(days: int = 0):
    """What a cleanup at this cutoff would reclaim. ``days=0`` means everything."""
    cutoff = _cutoff(days)
    art_count, art_bytes = await aged_artifact_size(cutoff)
    log_count, log_bytes = await logs_size_before(cutoff)
    return {
        "artifacts": {"count": art_count, "bytes": art_bytes},
        "logs": {"count": log_count, "bytes": log_bytes},
        "db_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "free_bytes": free_bytes(),
    }


@router.post("/api/storage/cleanup")
async def api_storage_cleanup(data: CleanupRequest):
    """Evict artifacts and/or wipe Agent logs older than the cutoff, then
    compact. Serialized against the preset/snapshot machinery, which also
    rewrites the whole file."""
    cutoff = _cutoff(data.days)
    artifacts_evicted = bytes_freed = logs_wiped = 0
    async with maintenance_lock():
        if data.artifacts:
            artifacts_evicted, bytes_freed = await evict_older_than(cutoff)
        if data.logs:
            logs_wiped = await wipe_logs_older_than(cutoff)
        before = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        compacted = await asyncio.to_thread(vacuum_sync)
        after = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {
        "artifacts_evicted": artifacts_evicted,
        "logs_wiped": logs_wiped,
        # What the user actually got back on disk. Falls back to the eviction's
        # own byte count when the VACUUM lost its race, since the pages are
        # freed either way -- just not returned to the OS until the next boot.
        "bytes_reclaimed": max(before - after, 0) if compacted else bytes_freed,
        "compacted": compacted,
    }
