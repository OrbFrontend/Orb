"""Character-card CRUD, import/export, and external-source proxy routes."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import tempfile
import uuid
import zipfile
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ...core import scrub_log
from ...database import (
    create_character_card,
    create_lorebook_entry,
    create_world,
    delete_character_card,
    delete_character_expressions,
    get_character_avatar,
    get_character_card,
    get_character_expression,
    get_lorebook_entries,
    get_user_persona,
    get_world,
    get_world_by_name,
    list_character_cards,
    list_expression_labels,
    set_character_expressions,
    sync_conversations_for_card,
    update_character_card,
)
from ...features.cards import downloader as card_downloader
from ...features.cards import parsing as tavern_cards
from ...inference.local_ml import (
    GO_EMOTIONS,  # dep-free tuple; importing triggers no llama import
)
from ..deps import _normalise_lorebook_entry
from ..schemas import CharacterCardCreate, CharacterCardUpdate, ImportUrlRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/characters")
async def api_list_characters():
    return await list_character_cards()


@router.post("/api/characters")
async def api_create_character(data: CharacterCardCreate):
    card_data = data.model_dump()
    card_data["id"] = card_data.get("id") or str(uuid.uuid4())
    card_data["source_format"] = card_data.get("source_format") or "manual"

    character_book = card_data.pop("character_book", None)
    if character_book and not card_data.get("world_id"):
        entries = character_book.get("entries") or []
        if isinstance(entries, dict):
            entries = list(entries.values())
        if entries:
            book_name = character_book.get("name") or card_data["name"]
            world = await get_world_by_name(book_name)
            if not world:
                world = await create_world({"name": book_name})
                for item in entries:
                    if isinstance(item, dict):
                        await create_lorebook_entry(world["id"], _normalise_lorebook_entry(item))
            card_data["world_id"] = world["id"]

    try:
        return await create_character_card(card_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/characters/import")
async def api_import_character(file: Annotated[UploadFile, File(...)]):
    """Import a SillyTavern-compatible character card PNG."""
    if not file.filename or not file.filename.lower().endswith(".png"):
        raise HTTPException(status_code=400, detail="Only .png character card files are supported")

    # Save to temp file for the parser
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Check for an embedded orb_id (card exported from this app) first so
        # that re-importing a previously exported card relinks conversation history.
        orb_id = tavern_cards.read_orb_id(tmp_path)
        card = tavern_cards.parse(tmp_path)
        card_dict = tavern_cards.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse tavern card")
        raise HTTPException(status_code=400, detail=f"Failed to parse character card: {e}") from e
    finally:
        os.unlink(tmp_path)

    # Determine stable card ID: prefer the embedded orb_id, fall back to SHA-256
    # of the raw PNG bytes so that reimporting the exact same file is idempotent.
    if orb_id:
        card_id = orb_id
    else:
        card_id = str(uuid.UUID(bytes=hashlib.sha256(content).digest()[:16], version=5))

    # Store the full PNG as the avatar
    avatar_b64 = base64.b64encode(content).decode("ascii")
    avatar_mime = "image/png"

    card_dict["id"] = card_id
    card_dict["avatar_b64"] = avatar_b64
    card_dict["avatar_mime"] = avatar_mime

    return card_dict


@router.get("/api/characters/browse")
async def api_browse_characters(source: str = "characterhub", q: str = "", page: int = 1):
    """Proxy external character-card search providers (avoids browser CORS)."""
    return await card_downloader.browse(source, q, page)


@router.get("/api/characters/randomize")
async def api_randomize_characters(source: str = "characterhub", q: str = ""):
    """Return a randomized selection from a source that supports randomize."""
    return await card_downloader.randomize(source, q)


@router.post("/api/characters/import-url")
async def api_import_character_url(req: ImportUrlRequest):
    """Download a character card from an external source and run it through the
    same parse pipeline as /api/characters/import."""
    return await card_downloader.download_card(req.source, req.full_path)


@router.get("/api/characters/{card_id}")
async def api_get_character(card_id: str):
    card = await get_character_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Character card not found")
    return card


@router.put("/api/characters/{card_id}")
async def api_update_character(card_id: str, data: CharacterCardUpdate):
    old_card = await get_character_card(card_id)
    update_data = data.model_dump(exclude_none=True)
    # world_id can be explicitly set to None to unlink; preserve it via model_fields_set
    if "world_id" in data.model_fields_set:
        update_data["world_id"] = data.world_id
    # persona_lock_id likewise: an explicit null clears the character lock
    if "persona_lock_id" in data.model_fields_set:
        # Migrated DBs carry no FK on the ALTER-added persona_lock_id column,
        # so the API is the only guard against locking to a missing persona.
        if data.persona_lock_id is not None and not await get_user_persona(data.persona_lock_id):
            raise HTTPException(status_code=400, detail="Persona not found")
        update_data["persona_lock_id"] = data.persona_lock_id
    result = await update_character_card(card_id, update_data)
    if not result:
        raise HTTPException(status_code=404, detail="Character card not found")
    old_name = old_card.get("name") if old_card and "name" in update_data else None
    await sync_conversations_for_card(card_id, result, old_name=old_name)
    return result


@router.delete("/api/characters/{card_id}")
async def api_delete_character(card_id: str, delete_conversations: bool = False):
    if not await delete_character_card(card_id, delete_conversations):
        raise HTTPException(status_code=404, detail="Character card not found")
    return {"ok": True}


@router.get("/api/characters/{card_id}/avatar")
async def api_get_avatar(card_id: str, request: Request):
    result = await get_character_avatar(card_id)
    if not result:
        raise HTTPException(status_code=404, detail="No avatar found")
    image_bytes, mime_type = result
    # Avatars are large (a card's full PNG) and change only on edit. Let the
    # browser cache them so the library grid doesn't re-download every avatar on
    # each re-render/search/sort. The frontend already busts the URL (?v=) when
    # an avatar is edited in-session; the ETag corrects cross-session edits once
    # max-age lapses via a cheap conditional GET. usedforsecurity=False: this is
    # a cache validator, not a security hash.
    etag = '"' + hashlib.md5(image_bytes, usedforsecurity=False).hexdigest() + '"'
    cache_headers = {"Cache-Control": "private, max-age=300", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)
    return Response(content=image_bytes, media_type=mime_type or "image/png", headers=cache_headers)


@router.get("/api/characters/{card_id}/export")
async def api_export_character(card_id: str):
    """Export a character card as a SillyTavern V2-compatible PNG."""
    card = await get_character_card(card_id, include_avatar=True)
    if not card:
        raise HTTPException(status_code=404, detail="Character not found")

    # Materialize a mutable working copy: the export augments the row with fields
    # that are not card columns (a forced ``id`` and an embedded ``character_book``),
    # so it is a free-form dict here rather than a CharacterCardRow.
    export_card: dict[str, Any] = dict(card)

    avatar_bytes: bytes | None = None
    avatar_b64 = export_card.get("avatar_b64")
    if avatar_b64:
        try:
            avatar_bytes = base64.b64decode(avatar_b64)
        except Exception:
            logger.warning("Avatar data for card %s is corrupt; exporting without avatar", scrub_log(card_id))
            avatar_bytes = None

    export_card["id"] = card_id

    # If the character is linked to a lorebook, embed it as character_book
    world_id = export_card.get("world_id")
    if world_id and not export_card.get("character_book"):
        world = await get_world(world_id)
        entries = await get_lorebook_entries(world_id)
        export_card["character_book"] = {
            "name": world["name"] if world else "",
            "extensions": {},
            "entries": [
                {
                    "keys": e["keywords"],
                    "content": e["content"],
                    "extensions": {},
                    "enabled": bool(e["enabled"]),
                    "insertion_order": e["sort_order"],
                    "case_sensitive": not bool(e["case_insensitive"]),
                    "constant": bool(e.get("constant", False)),
                    "name": e["name"],
                    "priority": e["priority"],
                    "id": e["id"],
                }
                for e in entries
            ],
        }

    png_bytes = tavern_cards.to_png(export_card, avatar_bytes)

    safe_name = "".join(c for c in export_card.get("name", "character") if c.isalnum() or c in " _-").strip() or "character"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.png"'},
    )


# ── Character expressions (SillyTavern-style expression packs) ────────────────

_EXPR_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_EXPR_GO_EMOTIONS = frozenset(GO_EMOTIONS)


def _extract_expressions(zip_bytes: bytes) -> dict[str, tuple[str, str]]:
    """Parse a zip of expression images → {label: (data_b64, mime)}.

    Flattens paths (basename), keeps files whose lowercase stem is a go-emotions
    label and whose extension is a known image type. Zip-bomb guards (trust
    boundary): reject > 200 entries or any declared entry > 5 MB before reading.
    """
    out: dict[str, tuple[str, str]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        infos = zf.infolist()
        if len(infos) > 200:
            raise ValueError("Zip has too many entries (max 200)")
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > 5 * 1024 * 1024:
                raise ValueError(f"Entry {info.filename!r} exceeds 5 MB")
            name = os.path.basename(info.filename)
            stem, _, ext = name.rpartition(".")
            label = stem.lower()
            mime = _EXPR_EXT_MIME.get(ext.lower())
            if not mime or label not in _EXPR_GO_EMOTIONS:
                continue
            out[label] = (base64.b64encode(zf.read(info)).decode("ascii"), mime)
    return out


@router.post("/api/characters/{card_id}/expressions")
async def api_upload_expressions(card_id: str, file: Annotated[UploadFile, File(...)]):
    """Upload a .zip of expression images; replaces the card's whole set."""
    if not await get_character_card(card_id):
        raise HTTPException(status_code=404, detail="Character card not found")
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Upload exceeds 50 MB")
    try:
        images = _extract_expressions(content)
    except (zipfile.BadZipFile, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Bad zip: {e}") from e
    if not images:
        raise HTTPException(status_code=400, detail="No files matched a go-emotions expression label")
    await set_character_expressions(card_id, images)
    return {"labels": sorted(images)}


@router.get("/api/characters/{card_id}/expressions")
async def api_list_expressions(card_id: str):
    return {"labels": await list_expression_labels(card_id)}


@router.get("/api/characters/{card_id}/expressions/{label}")
async def api_get_expression(card_id: str, label: str, request: Request):
    result = await get_character_expression(card_id, label)
    if not result:
        raise HTTPException(status_code=404, detail="No expression found")
    image_bytes, mime = result
    # Same private-cache + conditional-GET block as avatars: expressions change
    # only on re-upload, and the popup swaps src on label change without a buster.
    etag = '"' + hashlib.md5(image_bytes, usedforsecurity=False).hexdigest() + '"'
    cache_headers = {"Cache-Control": "private, max-age=300", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)
    return Response(content=image_bytes, media_type=mime or "image/png", headers=cache_headers)


@router.delete("/api/characters/{card_id}/expressions")
async def api_delete_expressions(card_id: str):
    await delete_character_expressions(card_id)
    return {"ok": True}
