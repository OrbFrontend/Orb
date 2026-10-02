"""Persistent dataset identity, independent of process lifetime."""

import uuid

from .. import connection
from ..connection import get_db

# Every API request reads the epoch, and only regenerate_dataset_epoch changes
# it, so it is cached per database file rather than re-read from SQLite.
_cached: tuple[str, str] | None = None


async def get_dataset_epoch() -> str:
    global _cached
    path = connection.DB_PATH
    if _cached is not None and _cached[0] == path:
        return _cached[1]
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT epoch FROM dataset_meta WHERE id = 1"))
        if not rows:
            await db.execute("INSERT OR IGNORE INTO dataset_meta (id, epoch) VALUES (1, ?)", (uuid.uuid4().hex,))
            await db.commit()
            rows = list(await db.execute_fetchall("SELECT epoch FROM dataset_meta WHERE id = 1"))
    _cached = (path, str(rows[0][0]))
    return _cached[1]


async def regenerate_dataset_epoch() -> str:
    global _cached
    epoch = uuid.uuid4().hex
    async with get_db() as db:
        await db.execute(
            "INSERT INTO dataset_meta (id, epoch) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET epoch = excluded.epoch", (epoch,)
        )
        await db.commit()
    _cached = (connection.DB_PATH, epoch)
    return epoch
