from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import DocumentListRow, DocumentRow


async def get_documents() -> list[DocumentListRow]:
    """List projection — never selects the full ``content`` (see DocumentListRow)."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall("SELECT id, title, created_at, updated_at FROM documents ORDER BY updated_at DESC")
        )
        return [cast(DocumentListRow, dict(r)) for r in rows]


async def get_document(document_id: str) -> DocumentRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM documents WHERE id = ?", (document_id,)))
        if not rows:
            return None
        d = dict(rows[0])
        d["generated_spans"] = json.loads(d["generated_spans"]) if d.get("generated_spans") else []
        return cast(DocumentRow, d)


async def create_document(data: dict) -> DocumentRow:
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        document_id = data.get("id") or str(uuid.uuid4())
        await db.execute(
            "INSERT INTO documents (id, title, content, generated_spans, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                document_id,
                data.get("title") or "Untitled",
                data.get("content", ""),
                json.dumps(data.get("generated_spans", [])),
                now,
                now,
            ),
        )
        await db.commit()
        result = await get_document(document_id)
        assert result is not None
        return result


async def update_document(document_id: str, data: dict) -> DocumentRow | None:
    async with get_db() as db:
        allowed = ["title", "content", "generated_spans"]
        sets, vals = _build_set_clause(allowed, data, json_fields={"generated_spans"})
        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(document_id)
            await db.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id = ?", vals)  # nosec B608
            await db.commit()
        return await get_document(document_id)


async def delete_document(document_id: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        await db.commit()
        return cur.rowcount > 0
