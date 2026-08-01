"""Re-encode ComfyUI's full-res PNG to WebP before it is stored.

ComfyUI hands back a full-resolution PNG -- a 1472x2304 render is ~4.7 MB, and a
chat that keeps eight of them inlines ~33 MB of base64 into one message payload,
which the browser must parse on every open. WebP at high quality is visually
identical and roughly 6x smaller, so far less base64 ships and gets parsed.

Resolution is preserved (no downscale) -- the stored image is still full-res, so
what shows inline is the full-quality picture. The per-image *decode* cost is set
by pixel count regardless of format; that is handled on the frontend by
content-visibility, which only decodes the messages actually on screen.

Lossy (q90, visually indistinguishable) and one-way -- the exact PNG bytes are not
kept. Reroll/rehydrate re-render from ComfyUI, so replay fidelity is unaffected.
"""

from __future__ import annotations

import io

from PIL import Image

_WEBP_QUALITY = 95
# Above either of these a reference is re-encoded before upload. Both are ceilings
# on a *phone-sized* upload, not a target size: identity-edit workflows are exactly
# the ones that lose face detail to a downscale, and a graph that wants a specific
# size already carries a scale node (ImageScaleToTotalPixels, GetImageSize).
_REFERENCE_MAX_EDGE = 4096
_REFERENCE_MAX_BYTES = 8 * 1024 * 1024


def shrink_for_display(data: bytes, mime: str) -> tuple[bytes, str]:
    """Re-encode to WebP at full resolution.

    Returns ``(webp_bytes, "image/webp")``, or the input ``(data, mime)`` unchanged
    when re-encoding would not help or fails. Never raises: a display optimization
    must not sink a generation that already cost a minute of GPU time.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=_WEBP_QUALITY, method=4)
    except Exception:
        return data, mime
    out = buf.getvalue()
    # An already-small source can encode larger as WebP -- keep whichever is smaller.
    return (out, "image/webp") if len(out) < len(data) else (data, mime)


def normalize_reference(data: bytes, mime: str) -> tuple[bytes, str]:
    """Bound a reference image before it is uploaded to ComfyUI.

    Returns the input unchanged unless the image is genuinely oversized -- a
    12 MP camera upload, not a render. Same never-raises discipline as
    ``shrink_for_display``: a reference Orb cannot re-encode is still a reference
    ComfyUI can probably load, so a failure here degrades to sending the original
    rather than sinking the generation.
    """
    if len(data) <= _REFERENCE_MAX_BYTES:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if max(probe.size) <= _REFERENCE_MAX_EDGE:
                    return data, mime
        except Exception:
            return data, mime
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.thumbnail((_REFERENCE_MAX_EDGE, _REFERENCE_MAX_EDGE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=_WEBP_QUALITY, method=4)
    except Exception:
        return data, mime
    out = buf.getvalue()
    return (out, "image/webp") if len(out) < len(data) else (data, mime)
