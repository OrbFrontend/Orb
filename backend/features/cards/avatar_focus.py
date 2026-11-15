"""Choose the square an avatar thumbnail crops to."""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image, ImageOps

from ...inference.local_models import head_detector
from ...inference.local_models.head_detector import Head

FOCUS_FEATURE = head_detector.FEATURE
#: Bump when the detector or the rule below changes what a stored focus would be.
FOCUS_REVISION = 1

Focus = tuple[float, float, float, float]

_SOLO_MARGIN = 1.35
_GROUP_MARGIN = 1.15
#: Heads at least this share of the main one's size are part of the shot, not background.
_GROUP_SHARE = 0.6
#: Smallest crop side in source pixels, so a small head is not blown up past the thumbnail.
_MIN_SIDE = 160


def focus_ready() -> bool:
    return head_detector.ready()[0]


def auto_focus_stamp(updated_at: str) -> str:
    return f"{FOCUS_REVISION}:{updated_at}"


def focus_square(width: int, height: int, heads: Sequence[Head]) -> Focus | None:
    """The crop for an image with these *heads*, in fractions of its size; None without heads."""
    if not heads or width <= 0 or height <= 0:
        return None
    main = max(heads, key=lambda h: h.score * h.side)
    shot = [h for h in heads if h.side >= main.side * _GROUP_SHARE]
    left, top = min(h.left for h in shot), min(h.top for h in shot)
    right, bottom = max(h.right for h in shot), max(h.bottom for h in shot)
    margin = _SOLO_MARGIN if len(shot) == 1 else _GROUP_MARGIN
    short = min(width, height)
    side = min(short, max(max(right - left, bottom - top) * margin, _MIN_SIDE))
    x = min(max(0.0, (left + right - side) / 2), width - side)
    y = min(max(0.0, (top + bottom - side) / 2), height - side)
    return (x / width, y / height, (x + side) / width, (y + side) / height)


def detect_focus(data: bytes) -> Focus | None:
    """The focus of an avatar's bytes; None when no head is found or the image is animated or unreadable."""
    try:
        with Image.open(io.BytesIO(data)) as src:
            if getattr(src, "is_animated", False):
                return None
            image = ImageOps.exif_transpose(src).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return focus_square(image.width, image.height, head_detector.heads(image))
