from __future__ import annotations

import json
from typing import cast

from ..connection import get_db
from ..models import DirectorStateRow


async def get_director_state(cid: str) -> DirectorStateRow:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM director_state WHERE conversation_id = ?", (cid,)))
        if rows:
            r = dict(rows[0])
            r["active_moods"] = json.loads(r["active_moods"])
            # Handle keywords column (may be missing in older DBs)
            if "keywords" in r and r["keywords"]:
                r["keywords"] = json.loads(r["keywords"])
            else:
                r["keywords"] = []
            # Handle progressive_fields column (may be missing in older DBs)
            raw_pf = r.get("progressive_fields")
            r["progressive_fields"] = json.loads(raw_pf) if raw_pf else {}
            return cast(DirectorStateRow, r)
        return {
            "conversation_id": cid,
            "active_moods": [],
            "keywords": [],
            "progressive_fields": {},
        }


async def update_director_state(
    cid: str,
    active_moods: list,
    keywords: list | None = None,
    progressive_fields: dict | None = None,
):
    async with get_db() as db:
        if keywords is not None:
            await db.execute(
                "UPDATE director_state SET active_moods = ?, keywords = ?, progressive_fields = ? WHERE conversation_id = ?",
                (
                    json.dumps(active_moods),
                    json.dumps(keywords),
                    json.dumps(progressive_fields or {}),
                    cid,
                ),
            )
        else:
            await db.execute(
                "UPDATE director_state SET active_moods = ?, progressive_fields = ? WHERE conversation_id = ?",
                (json.dumps(active_moods), json.dumps(progressive_fields or {}), cid),
            )
        await db.commit()
