"""Small square avatar thumbnails for list surfaces.

Every list shows an avatar as a circle of at most 80 CSS px, and the stored avatar is usually the whole card PNG: hundreds of
KB, sometimes 10 MB, with the card JSON embedded. On the live library, thumbnails cut the avatars from 282 MB to 2.8 MB, and a
thumbnail takes about 8 ms to make at the median.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image, ImageOps

#: The thumbnail's edge in pixels, big enough for an 80 px circle at 2x or a 60 px one at 3x.
THUMB_EDGE = 192


def _crop_box(width: int, height: int, focus: Sequence[float] | None) -> tuple[float, float, float, float]:
    side = float(min(width, height))
    cx, cy = width / 2, height / 2
    if focus:
        left, top, right, bottom = focus[0] * width, focus[1] * height, focus[2] * width, focus[3] * height
        side = max(1.0, min(side, right - left, bottom - top))
        cx, cy = (left + right) / 2, (top + bottom) / 2
    x = min(max(0.0, cx - side / 2), width - side)
    y = min(max(0.0, cy - side / 2), height - side)
    return (x, y, x + side, y + side)


def avatar_thumbnail(data: bytes, focus: Sequence[float] | None = None) -> bytes | None:
    """A ``THUMB_EDGE``-square WebP of *data*, cropped to *focus* or to the center the way ``object-fit: cover`` crops.

    *focus* is ``(left, top, right, bottom)`` in fractions of the image. None means the caller should serve the original. That
    covers an animated image, since a still frame would stop the animation in every list. It also covers bytes Pillow cannot
    decode, and without a focus a source already smaller than its thumbnail.
    """
    try:
        with Image.open(io.BytesIO(data)) as src:
            if getattr(src, "is_animated", False):
                return None
            # JPEG decodes at a reduced scale when asked, which is most of the
            # cost for a large photo. Other formats ignore the request.
            share = max(0.01, min(focus[2] - focus[0], focus[3] - focus[1], 1.0)) if focus else 1.0
            need = round(THUMB_EDGE / share)
            src.draft("RGB", (need, need))
            image = ImageOps.exif_transpose(src)
            has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            box = _crop_box(image.width, image.height, focus)
            edge = max(1, min(round(box[2] - box[0]), THUMB_EDGE))
            thumb = image.resize((edge, edge), Image.Resampling.LANCZOS, box=box, reducing_gap=3.0)
            out = io.BytesIO()
            thumb.save(out, format="WEBP", quality=80, method=4)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    body = out.getvalue()
    return body if focus or len(body) < len(data) else None
