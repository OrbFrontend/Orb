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
            # Per-conversation {{random}} picks for fragment text
            raw_mc = r.get("macro_choices")
            r["macro_choices"] = json.loads(raw_mc) if raw_mc else {}
            return cast(DirectorStateRow, r)
        return {
            "conversation_id": cid,
            "active_moods": [],
            "keywords": [],
            "progressive_fields": {},
            "macro_choices": {},
        }


async def update_director_state(
    cid: str,
    active_moods: list,
    keywords: list | None = None,
    progressive_fields: dict | None = None,
    macro_choices: dict | None = None,
):
    """Update the conversation's director state. ``keywords`` and
    ``macro_choices`` are left untouched when ``None``."""
    sets = ["active_moods = ?", "progressive_fields = ?"]
    vals: list = [json.dumps(active_moods), json.dumps(progressive_fields or {})]
    if keywords is not None:
        sets.append("keywords = ?")
        vals.append(json.dumps(keywords))
    if macro_choices is not None:
        sets.append("macro_choices = ?")
        vals.append(json.dumps(macro_choices))
    vals.append(cid)
    async with get_db() as db:
        await db.execute(
            f"UPDATE director_state SET {', '.join(sets)} WHERE conversation_id = ?",
            vals,  # nosec B608 — columns from a hardcoded list, values parameterised
        )
        await db.commit()
