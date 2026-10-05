"""Extract and fetch character expression packs."""

from __future__ import annotations

import base64
import io
import logging
import os
import zipfile
from collections.abc import Mapping

import httpx

from ...inference.local_ml import GO_EMOTIONS  # dep-free tuple; no llama import

logger = logging.getLogger(__name__)

_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_GO_EMOTIONS = frozenset(GO_EMOTIONS)
_MAX_ENTRY = 5 * 1024 * 1024
_MAX_ENTRIES = 200
_MAX_PACK = 50 * 1024 * 1024


def extract_expressions_zip(zip_bytes: bytes) -> dict[str, tuple[str, str]]:
    """Parse a zip of expression images -> {label: (data_b64, mime)}.

    Flattens paths (basename), keeps files whose lowercase stem is a go-emotions label and whose extension is a known image
    type. Zip-bomb guards (trust boundary): reject > 200 entries, any declared
    entry > 5 MB, or > 50 MB total uncompressed bytes before reading.
    """
    out: dict[str, tuple[str, str]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError("Zip has too many entries (max 200)")
        if sum(info.file_size for info in infos) > _MAX_PACK:
            raise ValueError("Zip exceeds 50 MB uncompressed")
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > _MAX_ENTRY:
                raise ValueError(f"Entry {info.filename!r} exceeds 5 MB")
            name = os.path.basename(info.filename)
            stem, _, ext = name.rpartition(".")
            label = stem.lower()
            mime = _EXT_MIME.get(ext.lower())
            if not mime or label not in _GO_EMOTIONS:
                continue
            out[label] = (base64.b64encode(zf.read(info)).decode("ascii"), mime)
    return out


def _expression_pack(card_dict: Mapping[str, object]) -> dict | None:
    """The chub-style ``{compressed, expressions}`` pack from a card's V2 extensions, if any."""
    ext = card_dict.get("extensions")
    chub = ext.get("chub") if isinstance(ext, dict) else None
    pack = chub.get("expressions") if isinstance(chub, dict) else None
    return pack if isinstance(pack, dict) else None


async def _fetch_limited(client: httpx.AsyncClient, url: str, limit: int) -> tuple[bytes, str] | None:
    """Stop consuming a response as soon as its decoded bytes exceed the limit."""
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(content) + len(chunk) > limit:
                return None
            content.extend(chunk)
        mime = (response.headers.get("content-type") or "image/png").split(";")[0] or "image/png"
        return bytes(content), mime


async def fetch_embedded_expressions(card_dict: Mapping[str, object]) -> dict[str, tuple[str, str]]:
    """Best-effort expression import: prefer the compressed zip, then per-label URLs.

    Return {} on absence/failure; card import must still succeed. Remote URLs are HTTPS-only and size-capped.
    """
    pack = _expression_pack(card_dict)
    if not pack:
        return {}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            zip_url = pack.get("compressed")
            if isinstance(zip_url, str) and zip_url.startswith("https://"):
                downloaded = await _fetch_limited(client, zip_url, _MAX_PACK)
                if downloaded is not None:
                    imgs = extract_expressions_zip(downloaded[0])
                    if imgs:
                        return imgs
            # Fallback: per-label image URLs.
            urls = pack.get("expressions")
            if not isinstance(urls, dict):
                return {}
            out: dict[str, tuple[str, str]] = {}
            for label, url in urls.items():
                lab = str(label).lower()
                if lab not in _GO_EMOTIONS or not (isinstance(url, str) and url.startswith("https://")):
                    continue
                downloaded = await _fetch_limited(client, url, _MAX_ENTRY)
                if downloaded is not None:
                    content, mime = downloaded
                    out[lab] = (base64.b64encode(content).decode("ascii"), mime)
            return out
    except (httpx.HTTPError, zipfile.BadZipFile, ValueError, OSError) as e:
        logger.warning("Expression auto-import failed: %s", e)
        return {}
