"""Small square avatar thumbnails for list surfaces.

Every list shows an avatar as a circle of at most 80 CSS px, and the stored
avatar is usually the whole card PNG: hundreds of KB, sometimes 10 MB, with the
card JSON embedded. On the live library, thumbnails cut the avatars from 282 MB
to 2.8 MB, and a thumbnail takes about 8 ms to make at the median.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

#: The thumbnail's edge in pixels, big enough for an 80 px circle at 2x or a 60 px one at 3x.
THUMB_EDGE = 192


def avatar_thumbnail(data: bytes) -> bytes | None:
    """A ``THUMB_EDGE``-square WebP of *data*, center-cropped the way ``object-fit: cover`` crops.

    None means the caller should serve the original. That covers an animated
    image, since a still frame would stop the animation in every list. It also
    covers bytes Pillow cannot decode, and a source already smaller than its thumbnail.
    """
    try:
        with Image.open(io.BytesIO(data)) as src:
            if getattr(src, "is_animated", False):
                return None
            # JPEG decodes at a reduced scale when asked, which is most of the
            # cost for a large photo. Other formats ignore the request.
            src.draft("RGB", (THUMB_EDGE, THUMB_EDGE))
            image = ImageOps.exif_transpose(src)
            has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            width, height = image.size
            side = min(width, height)
            box = ((width - side) / 2, (height - side) / 2, (width + side) / 2, (height + side) / 2)
            edge = min(side, THUMB_EDGE)
            thumb = image.resize((edge, edge), Image.Resampling.LANCZOS, box=box, reducing_gap=3.0)
            out = io.BytesIO()
            thumb.save(out, format="WEBP", quality=80, method=4)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    body = out.getvalue()
    return body if len(body) < len(data) else None
