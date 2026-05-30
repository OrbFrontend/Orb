"""Download character cards from external sources (CharacterHub, etc.).

Use a registry pattern so new sources can be added by calling register_source()
with a name, a browse function, a download function and a randomize function.
"""

from __future__ import annotations
import hashlib
import uuid
import logging
import base64
import random
import tempfile
import os

import httpx
from fastapi import HTTPException

from . import tavern_cards

logger = logging.getLogger(__name__)

_CHUB_PAGE_SIZE = 24
_CHUB_AVATARS_BASE = "https://avatars.charhub.io/avatars"
_CHUB_RANDOM_MAX_PAGE = 40

SOURCES: dict[str, dict] = {}


def register_source(name: str, browse_fn, download_fn, randomize_fn):
    """Register an external source for character-card browsing and downloading."""
    SOURCES[name] = {
        "browse": browse_fn,
        "download": download_fn,
        "randomize": randomize_fn,
    }


def _get_source(source: str) -> dict:
    src = SOURCES.get(source)
    if not src:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")
    return src


async def browse(source: str, q: str = "", page: int = 1) -> dict:
    """Proxy external character-card search providers (avoids browser CORS)."""
    return await _get_source(source)["browse"](q, page)


async def randomize(source: str, q: str = "") -> dict:
    """Return a randomized selection from a source."""
    return await _get_source(source)["randomize"](q)


async def download_card(source: str, full_path: str) -> dict:
    """Download and parse a character card from an external source.

    Returns the same dict shape as the file-import endpoint so the frontend
    can feed it straight into the character editor modal.
    """
    card_dict, avatar_b64, avatar_mime, card_id = await _get_source(source)["download"](full_path)
    card_dict["id"] = card_id
    if avatar_b64:
        card_dict["avatar_b64"] = avatar_b64
        card_dict["avatar_mime"] = avatar_mime
    return card_dict


# ── CharacterHub ──────────────────────────────────────────────────────


async def _chub_search(q: str, page: int) -> dict:
    """Run a CharacterHub search and normalize the response shape."""
    params = {
        "search": q,
        "page": max(1, int(page)),
        "sort": "download_count",
        "first": _CHUB_PAGE_SIZE,
        "nsfw": "true",
        "nsfl": "true",
        "asc": "false",
        "venus": "true",
    }
    url = "https://api.chub.ai/search"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.exception("CharacterHub search failed")
        raise HTTPException(status_code=502, detail=f"CharacterHub search failed: {e}") from e

    nodes = data.get("nodes") or data.get("data", {}).get("nodes") or []
    results = []
    for n in nodes:
        full_path = n.get("fullPath") or n.get("full_path") or ""
        avatar_url = n.get("avatar_url") or n.get("max_res_url")
        if not avatar_url and full_path:
            avatar_url = f"https://avatars.charhub.io/avatars/{full_path}/avatar.webp"
        topics = n.get("topics") or n.get("tags") or []
        if not isinstance(topics, list):
            topics = []
        date_updated = n.get("lastActivityAt") or n.get("last_activity_at") or n.get("createdAt") or n.get("created_at") or ""
        results.append(
            {
                "name": n.get("name", ""),
                "tagline": n.get("tagline", "") or n.get("description", "")[:140],
                "avatar_url": avatar_url,
                "full_path": full_path,
                "topics": topics,
                "date_updated": date_updated,
            }
        )
    has_more = len(nodes) >= _CHUB_PAGE_SIZE
    return {"results": results, "has_more": has_more}


async def _randomize_characterhub(q: str) -> dict:
    """Surface a random page of CharacterHub results.

    CharacterHub has no native "random" sort, so we jump to a random page of
    the (optionally query-filtered) catalog to give a fresh selection each call.
    """
    page = random.randint(1, _CHUB_RANDOM_MAX_PAGE)
    data = await _chub_search(q, page)
    # A random deep page can land past the end of the catalog; fall back to the
    # first page so the user still sees something rather than an empty grid.
    if not data["results"] and page > 1:
        data = await _chub_search(q, 1)
    # Randomized results are a one-shot batch; paging "Load More" would silently
    # switch back to ranked order, so don't advertise more.
    data["has_more"] = False
    return data


async def _download_characterhub_card(full_path: str):
    """Download the PNG character card from CharacterHub's CDN and parse it
    through the same tavern_cards pipeline as file import.

    Returns (card_dict, avatar_b64, avatar_mime, card_id).
    """
    if not full_path:
        raise HTTPException(status_code=400, detail="Missing full_path")
    if "/" not in full_path:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid CharacterHub full_path (expected creator/name): {full_path}",
        )
    url = f"{_CHUB_AVATARS_BASE}/{full_path}/chara_card_v2.png"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
    except httpx.HTTPError as e:
        logger.exception("Failed to download CharacterHub card PNG")
        raise HTTPException(status_code=502, detail=f"Failed to download card: {e}") from e

    if not content[:8].startswith(b"\x89PNG"):
        raise HTTPException(status_code=400, detail="Downloaded file does not appear to be a PNG card")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        orb_id = tavern_cards.read_orb_id(tmp_path)
        card = tavern_cards.parse(tmp_path)
        card_dict = tavern_cards.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse tavern card from CharacterHub")
        raise HTTPException(status_code=400, detail=f"Failed to parse character card: {e}") from e
    finally:
        os.unlink(tmp_path)

    card_id = orb_id if orb_id else str(uuid.UUID(bytes=hashlib.sha256(content).digest()[:16], version=5))
    avatar_b64 = base64.b64encode(content).decode("ascii")
    avatar_mime = "image/png"

    return card_dict, avatar_b64, avatar_mime, card_id


register_source(
    "characterhub",
    _chub_search,
    _download_characterhub_card,
    _randomize_characterhub,
)


# ── Character Archive (chararc.bernkastel.pictures) ───────────────────
#
# Character Archive mirrors cards from upstream sites (chub, etc.) behind a
# FastAPI JSON API. Browse hits the meilisearch-backed search endpoint; the
# per-card definition is served as chara_card_v2 JSON (not embedded in a PNG),
# so download parses it via tavern_cards.from_json_obj and fetches the avatar
# image separately.

_CHARARC_BASE = "https://chararc.bernkastel.pictures"
_CHARARC_API = f"{_CHARARC_BASE}/api/archive"
# Search caps page size at 20.
_CHARARC_PAGE_SIZE = 20
# Empty-query search exposes ~10k results (500 pages of 20) before the upstream
# offset cap returns empty; stay well within so a random page reliably has hits.
_CHARARC_RANDOM_MAX_PAGE = 250


def _chararc_full_path_str(src_obj: dict) -> str | None:
    """Extract a `creator/slug` path from a source-specific object."""
    fp = src_obj.get("fullPath") or src_obj.get("full_path")
    if isinstance(fp, list):
        return "/".join(str(p) for p in fp if p)
    if isinstance(fp, str):
        return fp.strip("/")
    return None


def _chararc_card_token(item: dict) -> str | None:
    """Build the `source/def/type/path` token used to fetch a card definition.

    chub cards carry their creator/slug under `chub.fullPath`; other upstreams
    expose it under `sourceSpecific`.
    """
    source = item.get("source")
    if not source:
        return None
    ctype = item.get("type") or "character"
    src_obj = item.get(source)
    if not isinstance(src_obj, dict):
        src_obj = item.get("sourceSpecific")
    if not isinstance(src_obj, dict):
        return None
    path = _chararc_full_path_str(src_obj)
    if not path:
        return None
    return f"{source}/def/{ctype}/{path}"


def _chararc_avatar_url(item: dict) -> str | None:
    """Best-effort thumbnail URL for a browse result (chub CDN for chub cards)."""
    src_obj = item.get("chub")
    if isinstance(src_obj, dict):
        path = _chararc_full_path_str(src_obj)
        if path:
            return f"https://avatars.charhub.io/avatars/{path}/avatar.webp"
    return None


def _chararc_to_result(item: dict) -> dict | None:
    """Normalize a search/random API item into the browse-result shape."""
    token = _chararc_card_token(item)
    if not token:
        return None
    tags = item.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {
        "name": item.get("name", ""),
        "tagline": item.get("tagline", "") or "",
        "avatar_url": _chararc_avatar_url(item),
        "full_path": token,
        "topics": tags,
        "date_updated": item.get("updated") or item.get("created") or item.get("added") or "",
    }


async def _browse_chararc(q: str, page: int) -> dict:
    page = max(1, int(page))
    params = {"query": q or "", "page": page, "count": _CHARARC_PAGE_SIZE}
    url = f"{_CHARARC_API}/v3/search/query"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.exception("Character Archive search failed")
        raise HTTPException(status_code=502, detail=f"Character Archive search failed: {e}") from e

    items = data.get("result") or []
    results = [r for r in (_chararc_to_result(i) for i in items if isinstance(i, dict)) if r]
    total_pages = data.get("totalPages") or 0
    return {"results": results, "has_more": page < total_pages}


async def _randomize_chararc(q: str) -> dict:
    """Surface a random batch of cards from Character Archive.

    The upstream ``random-character-ultra`` feed is reliable but extremely slow
    (~20s per call, regardless of batch size). The meilisearch-backed search
    endpoint responds in well under a second, so — like the CharacterHub
    randomizer — we jump to a random page of the (optionally query-filtered)
    catalog to give a fresh selection each call. One-shot batch.
    """
    page = random.randint(1, _CHARARC_RANDOM_MAX_PAGE)
    data = await _browse_chararc(q, page)
    # A deep random page can land past the end of a (query-filtered) result set;
    # fall back to the first page so the user still sees something.
    if not data["results"] and page > 1:
        data = await _browse_chararc(q, 1)
    # Randomized results are a one-shot batch; paging "Load More" would silently
    # switch back to ranked order, so don't advertise more.
    data["has_more"] = False
    return data


async def _download_chararc_card(token: str):
    """Download a Character Archive card definition (JSON) and its avatar.

    Returns (card_dict, avatar_b64, avatar_mime, card_id).
    """
    if not token:
        raise HTTPException(status_code=400, detail="Missing card path")
    # `token` is the `source/def/type/path` value produced by browse. Guard
    # against path traversal and accidental absolute URLs before interpolating.
    token = token.strip().strip("/")
    if not token or ".." in token or "://" in token:
        raise HTTPException(status_code=400, detail=f"Invalid card path: {token}")

    url = f"{_CHARARC_API}/v1/{token}"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            definition = resp.json()
    except httpx.HTTPError as e:
        logger.exception("Failed to download Character Archive definition")
        raise HTTPException(status_code=502, detail=f"Failed to download card: {e}") from e

    if not isinstance(definition, dict):
        raise HTTPException(status_code=400, detail="Unexpected card definition format")

    try:
        card = tavern_cards.from_json_obj(definition)
        card_dict = tavern_cards.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse Character Archive card definition")
        raise HTTPException(status_code=400, detail=f"Failed to parse character card: {e}") from e

    data = definition.get("data")
    if not isinstance(data, dict):
        data = {}
    # chub definitions expose example dialogue under a non-spec key; the V2
    # parser drops it, so carry it over to mes_example when that's empty.
    if not card_dict.get("mes_example") and data.get("example_dialogue"):
        card_dict["mes_example"] = data["example_dialogue"]

    # Pull the avatar image (a CDN URL embedded in the definition). Best effort:
    # a missing/broken avatar shouldn't block importing the card text.
    avatar_b64: str | None = None
    avatar_mime: str | None = None
    avatar_bytes = b""
    avatar_url = data.get("avatar")
    if isinstance(avatar_url, str) and avatar_url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                a = await client.get(avatar_url)
                a.raise_for_status()
                avatar_bytes = a.content
                avatar_mime = (a.headers.get("content-type") or "image/png").split(";")[0] or "image/png"
                avatar_b64 = base64.b64encode(avatar_bytes).decode("ascii")
        except httpx.HTTPError:
            logger.warning("Failed to fetch Character Archive avatar from %s", avatar_url)

    # Stable id so re-importing the same card relinks history: hash the avatar
    # bytes when present, else the card path.
    seed = avatar_bytes if avatar_bytes else token.encode("utf-8")
    card_id = str(uuid.UUID(bytes=hashlib.sha256(seed).digest()[:16], version=5))

    return card_dict, avatar_b64, avatar_mime, card_id


register_source(
    "chararc",
    _browse_chararc,
    _download_chararc_card,
    _randomize_chararc,
)
