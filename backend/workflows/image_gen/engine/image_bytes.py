"""Byte-level facts about a returned image, shared by every backend client.

Both the ComfyUI client and the cloud client have to decide the same two things
about bytes handed back by a remote server -- is this actually an image, and is
it small enough to store -- so the rule lives in one place rather than in each.
"""

from __future__ import annotations

from .contracts import ImageGenerationError

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def image_mime(data: bytes) -> str:
    """The image type, from magic bytes.

    Sniffed rather than trusted from `content-type`: the header is attacker- or
    misconfiguration-influenceable, and the value ends up on a stored attachment
    that the browser will render.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageGenerationError("The image backend returned data that is not a supported image")
