"""Persistent dataset identity, independent of process lifetime."""

import uuid

from ..connection import get_db


async def get_dataset_epoch() -> str:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT epoch FROM dataset_meta WHERE id = 1"))
        if not rows:
            await db.execute("INSERT OR IGNORE INTO dataset_meta (id, epoch) VALUES (1, ?)", (uuid.uuid4().hex,))
            await db.commit()
            rows = list(await db.execute_fetchall("SELECT epoch FROM dataset_meta WHERE id = 1"))
        return str(rows[0][0])


async def regenerate_dataset_epoch() -> str:
    epoch = uuid.uuid4().hex
    async with get_db() as db:
        await db.execute(
            "INSERT INTO dataset_meta (id, epoch) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET epoch = excluded.epoch",
            (epoch,),
        )
        await db.commit()
    return epoch
