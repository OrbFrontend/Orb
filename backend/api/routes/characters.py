"""Character-card CRUD, import/export, and external-source proxy routes."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import uuid
import zipfile
from typing import Annotated, Any, Literal

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
    get_character_usage,
    get_lorebook_entries,
    get_settings,
    get_user_persona,
    get_world,
    get_world_by_name,
    list_character_cards,
    list_expression_labels,
    set_character_expressions,
    set_public_profile,
    sync_conversations_for_card,
    update_character_card,
)
from ...features.cards import downloader as card_downloader
from ...features.cards import draft_card_profile
from ...features.cards import expressions as card_expressions
from ...features.cards import parsing as tavern_cards
from ...inference import agent_lane_from_settings, client_from_settings
from ..deps import (
    _normalise_lorebook_entry,
    cached_image_response,
    lorebook_to_book,
    profile_draft_failures,
    project_lorebook_view,
)
from ..schemas import (
    CharacterCardCreate,
    CharacterCardUpdate,
    ImportUrlRequest,
    PublicProfilePayload,
)

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
        created = await create_character_card(card_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Auto-import an embedded expression pack (chub extension) so cards imported
    # from the internet — or from a PNG that carries one — arrive with their
    # sprites, no manual zip upload. Best-effort: a no-op for cards without a pack.
    imgs = await card_expressions.fetch_embedded_expressions(card_data)
    if imgs:
        await set_character_expressions(card_data["id"], imgs)

    return created


@router.post("/api/characters/import")
async def api_import_character(file: Annotated[UploadFile, File(...)]):
    """Import a character card PNG (V3 chunk preferred, V2/V1 fallback)."""
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


@router.post("/api/characters/{card_id}/public-profile/generate")
async def api_generate_public_profile(card_id: str):
    """Return an editable draft; generation never overwrites the card.

    Raises rather than degrading: a plausible-looking profile built from the
    card's description under a "Draft ready" toast is worse than an error,
    because it is indistinguishable from a real answer.
    """
    card = await get_character_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Character card not found")
    settings = await get_settings()
    client = client_from_settings(settings)
    agent_client, model = agent_lane_from_settings(settings, writer_client=client)
    with profile_draft_failures(f"Public-profile generation for card {scrub_log(card_id)!r}"):
        return await draft_card_profile(agent_client, model or "", card)


@router.put("/api/characters/{card_id}/public-profile")
async def api_save_public_profile(card_id: str, data: PublicProfilePayload):
    card = await set_public_profile(card_id, data.appearance, data.role)
    if not card:
        raise HTTPException(status_code=404, detail="Character card not found")
    return (card.get("extensions") or {}).get("orb", {}).get("public_profile", {})


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


@router.get("/api/characters/{card_id}/usage")
async def api_character_usage(card_id: str):
    if not await get_character_card(card_id):
        raise HTTPException(status_code=404, detail="Character card not found")
    return await get_character_usage(card_id)


@router.get("/api/characters/{card_id}/avatar")
async def api_get_avatar(card_id: str, request: Request):
    result = await get_character_avatar(card_id)
    if not result:
        raise HTTPException(status_code=404, detail="No avatar found")
    image_bytes, mime_type = result
    # Avatars are large (a card's full PNG) and change only on edit; let the
    # browser cache them so the library grid doesn't re-download every avatar on
    # each re-render/search/sort.
    return cached_image_response(image_bytes, mime_type, request)


@router.get("/api/characters/{card_id}/export")
async def api_export_character(card_id: str, world_view: Literal["authored", "effective"] = "authored"):
    """Export a character card as a V2-compatible card PNG.

    The embedded ``character_book`` is the *authored* lorebook by default, so a
    card shared with someone else carries the lore its author wrote rather than
    whatever a particular playthrough's Agent proposed and its owner accepted.
    ``world_view=effective`` opts into exporting the projection instead.
    """
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
        entries = project_lorebook_view(await get_lorebook_entries(world_id), world_view)
        export_card["character_book"] = lorebook_to_book(world["name"] if world else "", entries)

    png_bytes = tavern_cards.to_png(export_card, avatar_bytes)

    safe_name = "".join(c for c in export_card.get("name", "character") if c.isalnum() or c in " _-").strip() or "character"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.png"'},
    )


@router.post("/api/characters/{card_id}/expressions")
async def api_upload_expressions(card_id: str, file: Annotated[UploadFile, File(...)]):
    """Upload a .zip of expression images; replaces the card's whole set."""
    if not await get_character_card(card_id):
        raise HTTPException(status_code=404, detail="Character card not found")
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Upload exceeds 50 MB")
    try:
        images = card_expressions.extract_expressions_zip(content)
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
    # Same private-cache + conditional-GET treatment as avatars: expressions
    # change only on re-upload, and the popup swaps src on label change without
    # a buster.
    return cached_image_response(image_bytes, mime, request)


@router.delete("/api/characters/{card_id}/expressions")
async def api_delete_expressions(card_id: str):
    await delete_character_expressions(card_id)
    return {"ok": True}
