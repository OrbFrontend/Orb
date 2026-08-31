"""Shared image-byte helpers."""

from __future__ import annotations

from .contracts import ImageGenerationError

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def image_mime(data: bytes) -> str:
    """The image type, from magic bytes. Sniffed rather than trusted from
    `content-type`: that header is attacker-influenceable, and the value ends up on
    a stored attachment the browser will render."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageGenerationError("The image backend returned data that is not a supported image")
