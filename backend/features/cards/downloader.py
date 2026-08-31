"""Download character cards from external sources."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import tempfile
import uuid

import httpx
from fastapi import HTTPException

from . import parsing

logger = logging.getLogger(__name__)

_CHUB_PAGE_SIZE = 24
_CHUB_AVATARS_BASE = "https://avatars.charhub.io/avatars"
# The search API serves at most 100k results and returns empty pages past them,
# whatever the sort or query — the ceiling for a random page.
_CHUB_MAX_RESULTS = 100_000
# The detail API 403s a bare "Mozilla/5.0"; a full browser UA passes.
_CHUB_SITE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
}

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


async def _fetch(
    url: str,
    *,
    what: str,
    params: dict | None = None,
    timeout: float = 30,
    headers: dict | None = None,
) -> httpx.Response:
    """GET *url*, mapping any transport or status failure to HTTP 502.

    The single outbound seam for every source's browse/randomize/download call.
    *what* is the human phrase for the operation ("Botbooru search failed",
    "Failed to download card"); it is both logged and returned as the
    client-facing detail, so each source keeps its own wording without
    repeating the request block. Body decoding stays with the caller — sources
    want ``.json()`` or ``.content`` and differ on how a malformed body maps to
    a status.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp
    except httpx.HTTPError as e:
        logger.exception("%s", what)
        raise HTTPException(status_code=502, detail=f"{what}: {e}") from e


def _parse_png_card(content: bytes, source_label: str) -> tuple[dict, str, str, str]:
    """Parse downloaded PNG card bytes through the same tavern_cards pipeline as file import.

    Returns ``(card_dict, avatar_b64, avatar_mime, card_id)`` — the PNG itself
    doubles as the avatar, and ``card_id`` is the embedded orb id when present,
    else a stable hash of the bytes so re-importing the same card relinks history.
    """
    if not content[:8].startswith(b"\x89PNG"):
        raise HTTPException(status_code=400, detail="Downloaded file does not appear to be a PNG card")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        orb_id = parsing.read_orb_id(tmp_path)
        card = parsing.parse(tmp_path)
        card_dict = parsing.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse tavern card from %s", source_label)
        raise HTTPException(status_code=400, detail=f"Failed to parse character card: {e}") from e
    finally:
        os.unlink(tmp_path)

    card_id = orb_id if orb_id else str(uuid.UUID(bytes=hashlib.sha256(content).digest()[:16], version=5))
    avatar_b64 = base64.b64encode(content).decode("ascii")
    return card_dict, avatar_b64, "image/png", card_id


async def _fetch_avatar(avatar_url: object, source_label: str) -> tuple[str | None, str | None, bytes]:
    """Best-effort fetch of a card's avatar image from a CDN URL.

    Returns ``(avatar_b64, avatar_mime, avatar_bytes)``. A missing or broken
    avatar degrades to ``(None, None, b"")`` — it must not block importing the
    card text.
    """
    if not (isinstance(avatar_url, str) and avatar_url.startswith(("http://", "https://"))):
        return None, None, b""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            a = await client.get(avatar_url)
            a.raise_for_status()
            mime = (a.headers.get("content-type") or "image/png").split(";")[0] or "image/png"
            return base64.b64encode(a.content).decode("ascii"), mime, a.content
    except httpx.HTTPError:
        logger.warning("Failed to fetch %s avatar from %s", source_label, avatar_url)
        return None, None, b""


async def _chub_page(q: str, page: int) -> tuple[dict, int]:
    """Fetch one CharacterHub search page as ``(payload, total_count)``.

    The count sizes the whole (query-filtered) result set, which the randomizer
    needs to know how deep it may jump; browse callers only want the payload.
    """
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
    payload = (await _fetch(url, what="CharacterHub search failed", params=params, timeout=15)).json()

    # Results come wrapped in a `data` envelope; tolerate a flat response too.
    body = payload if isinstance(payload.get("nodes"), list) else (payload.get("data") or {})
    nodes = body.get("nodes") or []
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
    return {"results": results, "has_more": has_more}, int(body.get("count") or 0)


async def _chub_search(q: str, page: int) -> dict:
    """Run a CharacterHub search and normalize the response shape."""
    data, _ = await _chub_page(q, page)
    return data


def _chub_max_page(count: int) -> int:
    """Deepest fully-populated search page for a result set of *count* items."""
    return max(1, min(count, _CHUB_MAX_RESULTS) // _CHUB_PAGE_SIZE)


async def _randomize_characterhub(q: str) -> dict:
    """Surface a random page of CharacterHub results.

    ``sort=random`` reseeds only every few minutes, so consecutive calls repeat;
    jumping to a random page of the (optionally query-filtered) catalog is what
    varies the batch. Card quality holds up across the whole ~4166-page window,
    so the pick spans all of it.

    A query narrows the catalog and the first pick can overshoot its end; the
    response's own count then gives the real range to re-pick from.
    """
    data, count = await _chub_page(q, random.randint(1, _chub_max_page(_CHUB_MAX_RESULTS)))
    if not data["results"] and count:
        data, _ = await _chub_page(q, random.randint(1, _chub_max_page(count)))
    # Randomized results are a one-shot batch; paging "Load More" would silently
    # switch back to ranked order, so don't advertise more.
    data["has_more"] = False
    return data


async def _chub_expression_pack(full_path: str) -> dict | None:
    """Best-effort fetch of a CharacterHub card's expression pack.

    The pack (``{compressed, expressions}``) lives only in the detail API — the
    CDN card PNG carries ``expressions: null`` — so we fetch it separately and
    let the caller merge it into the card's extensions. Never raises: expressions
    are a nice-to-have and must not block importing the card.
    """
    url = f"https://api.chub.ai/api/characters/{full_path}?full=true"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_CHUB_SITE_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            node = resp.json().get("node") or {}
        ext = (node.get("definition") or {}).get("extensions") or {}
        pack = (ext.get("chub") or {}).get("expressions")
        return pack if isinstance(pack, dict) else None
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Failed to fetch CharacterHub expression pack for %s: %s", full_path, e)
        return None


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
    content = (await _fetch(url, what="Failed to download card")).content

    card_dict, avatar_b64, avatar_mime, card_id = _parse_png_card(content, "CharacterHub")
    # The embedded card's expression pack is null; the detail API has it. Merge
    # it into extensions so the shared create-time auto-import can pick it up.
    pack = await _chub_expression_pack(full_path)
    if pack:
        ext = card_dict.setdefault("extensions", {})
        ext.setdefault("chub", {})["expressions"] = pack
    return card_dict, avatar_b64, avatar_mime, card_id


register_source(
    "characterhub",
    _chub_search,
    _download_characterhub_card,
    _randomize_characterhub,
)


#
# Character Archive mirrors cards from upstream sites (chub, etc.) behind a
# FastAPI JSON API. Browse hits the meilisearch-backed search endpoint; the
# per-card definition is served as chara_card_v2 JSON (not embedded in a PNG),
# so download parses it via parsing.from_json_obj and fetches the avatar
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
    data = (await _fetch(url, what="Character Archive search failed", params=params, timeout=20)).json()

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
    definition = (await _fetch(url, what="Failed to download card")).json()

    if not isinstance(definition, dict):
        raise HTTPException(status_code=400, detail="Unexpected card definition format")

    try:
        card = parsing.from_json_obj(definition)
        card_dict = parsing.card_to_dict(card)
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

    # Pull the avatar image (a CDN URL embedded in the definition).
    avatar_b64, avatar_mime, avatar_bytes = await _fetch_avatar(data.get("avatar"), "Character Archive")

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


#
# Botbooru serves standard tavern PNG cards (tEXt chara chunk) and exposes a
# JSON browse API whose `q` matches both tags and character names. Unlike the
# other two sources it has a native random sort, so the randomizer is a single
# query-filtered request rather than a random-page hack.

_BOTBOORU_BASE = "https://botbooru.com"
_BOTBOORU_PAGE_SIZE = 24


def _botbooru_to_result(post: dict) -> dict:
    """Normalize a Botbooru post into the standard browse-result shape."""
    tagline = post.get("tagline") or post.get("creator_notes_excerpt") or ""
    if len(tagline) > 140:
        tagline = tagline[:140]
    filename = post.get("filename")
    avatar_url = None
    if filename:
        avatar_url = f"{_BOTBOORU_BASE}/images/preview/480/{filename}?v={post.get('card_image_revision', '')}"
    tags = post.get("tags")
    if not isinstance(tags, list):
        tags = []
    topics = [t["name"] for t in tags if isinstance(t, dict) and t.get("name")]
    return {
        "name": post.get("character_name", ""),
        "tagline": tagline,
        "avatar_url": avatar_url,
        "full_path": str(post.get("id", "")),
        "topics": topics,
        "date_updated": post.get("created_at", ""),
    }


async def _botbooru_posts(params: dict, q: str, *, what: str) -> tuple[list[dict], int, int]:
    """Run one Botbooru ``/posts/`` query. Returns ``(results, fetched, total)``.

    ``fetched`` counts the raw posts, not the normalized results, so a
    malformed entry still advances the caller's paging arithmetic.
    """
    if q:
        params["q"] = q
    data = (await _fetch(f"{_BOTBOORU_BASE}/posts/", what=what, params=params, timeout=20)).json()
    posts = data.get("posts") or []
    return [_botbooru_to_result(p) for p in posts if isinstance(p, dict)], len(posts), int(data.get("total") or 0)


async def _browse_botbooru(q: str, page: int) -> dict:
    """Run a Botbooru browse query and normalize the response shape."""
    offset = (max(1, int(page)) - 1) * _BOTBOORU_PAGE_SIZE
    results, fetched, total = await _botbooru_posts(
        {"sort": "downloads", "limit": _BOTBOORU_PAGE_SIZE, "offset": offset}, q, what="Botbooru search failed"
    )
    return {"results": results, "has_more": offset + fetched < total}


async def _randomize_botbooru(q: str) -> dict:
    """Surface a random batch of cards from Botbooru.

    Botbooru has a native server-side random sort, so a single query-filtered
    request gives a fresh selection each call. Randomized results are a one-shot
    batch; paging "Load More" would silently switch back to ranked order, so
    don't advertise more.
    """
    results, _, _ = await _botbooru_posts({"sort": "random", "limit": _BOTBOORU_PAGE_SIZE}, q, what="Botbooru randomize failed")
    return {"results": results, "has_more": False}


async def _download_botbooru_card(full_path: str):
    """Download the PNG character card from Botbooru and parse it through the
    same tavern_cards pipeline as file import.

    Returns (card_dict, avatar_b64, avatar_mime, card_id).
    """
    if not full_path:
        raise HTTPException(status_code=400, detail="Missing full_path")
    # `full_path` is a numeric post id; guard against path injection into the URL.
    if not full_path.isdigit():
        raise HTTPException(status_code=400, detail=f"Invalid Botbooru post id: {full_path}")

    url = f"{_BOTBOORU_BASE}/download/png/{full_path}"
    content = (await _fetch(url, what="Failed to download card")).content

    return _parse_png_card(content, "Botbooru")


register_source(
    "botbooru",
    _browse_botbooru,
    _download_botbooru_card,
    _randomize_botbooru,
)


#
# Wyvern exposes an unauthenticated JSON explore API. The search endpoint
# already returns full card definitions (description/personality/first_mes/…),
# but only id references for lorebooks; the per-character endpoint embeds the
# lorebook entries, so download fetches that and converts them to a V2
# character_book. Avatars are served from a Cloudflare Images CDN. There is no
# native random sort, so the randomizer jumps to a random page — but unlike the
# other sources it reads the real page count first so it works for narrow
# queries too.

_WYVERN_BASE = "https://api.wyvern.chat"
_WYVERN_PAGE_SIZE = 24


def _wyvern_to_result(item: dict) -> dict:
    """Normalize a Wyvern character object into the standard browse-result shape."""
    tagline = item.get("tagline") or item.get("creator_notes") or ""
    if len(tagline) > 140:
        tagline = tagline[:140]
    tags = item.get("tags")
    if not isinstance(tags, list):
        tags = []
    topics = [t for t in tags if isinstance(t, str)]
    return {
        "name": item.get("name", "") or "",
        "tagline": tagline,
        "avatar_url": item.get("avatar") or None,
        "full_path": str(item.get("id") or item.get("_id") or ""),
        "topics": topics,
        "date_updated": item.get("updated_at") or item.get("created_at") or "",
    }


async def _wyvern_search(q: str, page: int) -> dict:
    """Run a Wyvern explore search and return the raw (parsed) JSON response."""
    page = max(1, int(page))
    params = {
        "page": page,
        "limit": _WYVERN_PAGE_SIZE,
        "sort": "created_at",
        "order": "DESC",
    }
    if q:
        params["q"] = q
    url = f"{_WYVERN_BASE}/exploreSearch/characters"
    data = (await _fetch(url, what="Wyvern search failed", params=params, timeout=20)).json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Unexpected Wyvern search response")
    return data


async def _browse_wyvern(q: str, page: int) -> dict:
    data = await _wyvern_search(q, page)
    items = data.get("results") or []
    results = [_wyvern_to_result(i) for i in items if isinstance(i, dict)]
    return {"results": results, "has_more": bool(data.get("hasMore"))}


async def _randomize_wyvern(q: str) -> dict:
    """Surface a random batch of cards from Wyvern.

    Wyvern has no native random sort, so — like the CharacterHub randomizer — we
    jump to a random page of the (optionally query-filtered) catalog. We first
    read the real ``totalPages`` so the random page is always in range, which
    keeps it working even when a query narrows the catalog to a handful of pages.
    """
    first = await _wyvern_search(q, 1)
    total_pages = int(first.get("totalPages") or 1)
    if total_pages <= 1:
        data = first
    else:
        data = await _wyvern_search(q, random.randint(1, total_pages))
    items = data.get("results") or []
    results = [_wyvern_to_result(i) for i in items if isinstance(i, dict)]
    # Randomized results are a one-shot batch; paging "Load More" would silently
    # switch back to ranked order, so don't advertise more.
    return {"results": results, "has_more": False}


def _wyvern_character_book(obj: dict) -> dict | None:
    """Convert Wyvern's embedded lorebooks into a single V2 character_book.

    A card may reference several lorebooks; the V2 spec allows only one, so we
    merge all of their entries. Only the spec-defined entry fields are carried
    over (Wyvern-specific keys like ``key_logic``/``sticky`` and the ambiguous
    numeric ``position`` are dropped so the V2 parser doesn't choke).
    """
    lorebooks = obj.get("lorebooks")
    if not isinstance(lorebooks, list):
        return None
    entries: list[dict] = []
    name = None
    description = None
    scan_depth = None
    token_budget = None
    recursive_scanning = None
    for lb in lorebooks:
        if not isinstance(lb, dict):
            continue
        if name is None:
            name = lb.get("name")
            description = lb.get("description")
            scan_depth = lb.get("scan_depth")
            token_budget = lb.get("token_budget")
            recursive_scanning = lb.get("recursive_scanning")
        for e in lb.get("entries") or []:
            if not isinstance(e, dict):
                continue
            keys = e.get("keys")
            entry = {
                "keys": keys if isinstance(keys, list) else [],
                "content": e.get("content", "") or "",
                "extensions": e.get("extensions") if isinstance(e.get("extensions"), dict) else {},
                "enabled": e.get("enabled", True),
                "insertion_order": e.get("insertion_order", 0) or 0,
            }
            for src_key in ("case_sensitive", "name", "priority", "comment", "secondary_keys", "constant"):
                if e.get(src_key) is not None:
                    entry[src_key] = e[src_key]
            entries.append(entry)
    if not entries:
        return None
    book: dict = {"entries": entries}
    if name is not None:
        book["name"] = name
    if description is not None:
        book["description"] = description
    if scan_depth is not None:
        book["scan_depth"] = scan_depth
    if token_budget is not None:
        book["token_budget"] = token_budget
    if recursive_scanning is not None:
        book["recursive_scanning"] = recursive_scanning
    return book


def _wyvern_to_v2_jobj(obj: dict) -> dict:
    """Build a chara_card_v2 JSON object from a Wyvern character object."""
    creator = obj.get("creator")
    creator_name = ""
    if isinstance(creator, dict):
        creator_name = creator.get("displayName") or creator.get("username") or ""
    tags = obj.get("tags")
    alt = obj.get("alternate_greetings")
    data: dict = {
        "name": obj.get("name", "") or "",
        "description": obj.get("description", "") or "",
        "personality": obj.get("personality", "") or "",
        "scenario": obj.get("scenario", "") or "",
        "first_mes": obj.get("first_mes", "") or "",
        "mes_example": obj.get("mes_example", "") or "",
        "creator_notes": obj.get("creator_notes", "") or "",
        # Wyvern splits the system prompt into pre/post-history instructions,
        # matching the card spec's system_prompt / post_history_instructions.
        "system_prompt": obj.get("pre_history_instructions", "") or "",
        "post_history_instructions": obj.get("post_history_instructions", "") or "",
        "alternate_greetings": alt if isinstance(alt, list) else [],
        "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        "creator": creator_name,
    }
    book = _wyvern_character_book(obj)
    if book:
        data["character_book"] = book
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


async def _download_wyvern_card(full_path: str):
    """Fetch a Wyvern character (full definition + embedded lorebooks) and its
    avatar, then parse it through the same tavern_cards pipeline as file import.

    Returns (card_dict, avatar_b64, avatar_mime, card_id).
    """
    if not full_path:
        raise HTTPException(status_code=400, detail="Missing character id")
    # `full_path` is the Wyvern character id; guard against path injection.
    char_id = full_path.strip().strip("/")
    if not char_id or "/" in char_id or ".." in char_id or "://" in char_id:
        raise HTTPException(status_code=400, detail=f"Invalid Wyvern character id: {full_path}")

    url = f"{_WYVERN_BASE}/characters/{char_id}"
    obj = (await _fetch(url, what="Failed to download card")).json()

    if not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="Unexpected Wyvern character format")

    try:
        card = parsing.from_json_obj(_wyvern_to_v2_jobj(obj))
        card_dict = parsing.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse Wyvern character")
        raise HTTPException(status_code=400, detail=f"Failed to parse character card: {e}") from e

    # Pull the avatar image (Cloudflare Images CDN URL).
    avatar_b64, avatar_mime, avatar_bytes = await _fetch_avatar(obj.get("avatar"), "Wyvern")

    # Stable id so re-importing the same card relinks history: hash the avatar
    # bytes when present, else the character id.
    seed = avatar_bytes if avatar_bytes else char_id.encode("utf-8")
    card_id = str(uuid.UUID(bytes=hashlib.sha256(seed).digest()[:16], version=5))

    return card_dict, avatar_b64, avatar_mime, card_id


register_source(
    "wyvern",
    _browse_wyvern,
    _download_wyvern_card,
    _randomize_wyvern,
)
