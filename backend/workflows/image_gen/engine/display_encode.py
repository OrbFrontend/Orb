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
# Ceilings on a *phone-sized* reference upload, not a target size: identity-edit
# workflows are the ones that lose face detail to a downscale, and a graph wanting
# a specific size already carries a scale node (ImageScaleToTotalPixels).
_REFERENCE_MAX_EDGE = 4096
_REFERENCE_MAX_BYTES = 8 * 1024 * 1024

_FORMATS = {"image/webp": "WEBP", "image/png": "PNG", "image/jpeg": "JPEG"}


def _reencode(
    data: bytes,
    mime: str,
    max_edge: int | None = None,
    *,
    target_mime: str = "image/webp",
    force: bool = False,
) -> tuple[bytes, str]:
    """Re-encode to `target_mime`, optionally bounded to `max_edge`.

    `force` is the difference between converting *for bytes* and converting *for
    compatibility*. Without it the smaller-wins branch below hands back the input
    format whenever the re-encode came out larger -- correct when the whole point
    was to save space, and silently wrong when the caller needed the format
    changed because the destination cannot read the input one.

    Never raises: re-encoding is an optimization, and returning the input
    unchanged is always a correct answer.
    """
    fmt = _FORMATS.get(target_mime, "WEBP")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            # JPEG has no alpha channel, so RGBA must be flattened rather than
            # handed to a save that would raise and fall back to the input.
            wanted = "RGB" if fmt == "JPEG" else ("RGB", "RGBA")
            if img.mode not in wanted:
                img = img.convert("RGB")
            if max_edge:
                img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            options: dict = {"quality": _WEBP_QUALITY}
            if fmt == "WEBP":
                options["method"] = 4
            img.save(buf, format=fmt, **options)
    except Exception:
        return data, mime
    out = buf.getvalue()
    if force:
        return out, target_mime
    # An already-small source can encode larger as WebP -- keep whichever is smaller.
    return (out, target_mime) if len(out) < len(data) else (data, mime)


def shrink_for_display(data: bytes, mime: str) -> tuple[bytes, str]:
    """Re-encode a render to WebP at full resolution, for storage and inlining."""
    return _reencode(data, mime)


def normalize_reference(
    data: bytes,
    mime: str,
    *,
    allowed: tuple[str, ...] = (),
    max_bytes: int = _REFERENCE_MAX_BYTES,
) -> tuple[bytes, str]:
    """Bound a reference image before it is sent to an image backend.

    Untouched unless genuinely oversized -- a 12 MP camera upload, not a render.

    `allowed` makes the conversion **mime-driven as well as size-driven**, which
    is what the cloud path needs and what the size gate alone cannot give it:
    `shrink_for_display` stores every render as WebP, so a reference resolving to
    the previous image is WebP and comfortably under both ceilings. It would sail
    through unconverted into a JSON body that told the provider PNG/JPEG. So a
    disallowed *input* mime forces the re-encode irrespective of size.

    The shared default is deliberately unchanged: ComfyUI accepts WebP fine, and
    `REFERENCE_MIMES` still describes what Orb accepts, not what every backend does.
    """
    target = "image/webp" if not allowed else ("image/png" if "image/png" in allowed else allowed[0])
    incompatible = bool(allowed) and mime not in allowed
    if not incompatible and len(data) <= max_bytes:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if max(probe.size) <= _REFERENCE_MAX_EDGE:
                    return data, mime
        except Exception:
            return data, mime
    return _reencode(data, mime, _REFERENCE_MAX_EDGE, target_mime=target, force=incompatible)
