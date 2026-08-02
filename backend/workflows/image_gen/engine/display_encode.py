"""Re-encode images on the way out (display) and on the way in (references).

**Display.** ComfyUI hands back a full-resolution PNG -- a 1472x2304 render is
~4.7 MB, and a chat that keeps eight of them inlines ~33 MB of base64 into one
message payload, which the browser must parse on every open. WebP at high quality
is visually identical and roughly 6x smaller, so far less base64 ships and gets
parsed. Resolution is preserved (no downscale) -- the stored image is still
full-res, so what shows inline is the full-quality picture. The per-image *decode*
cost is set by pixel count regardless of format; that is handled on the frontend
by content-visibility, which only decodes the messages actually on screen. Lossy
(q95, visually indistinguishable) and one-way -- the exact PNG bytes are not kept.
Reroll/rehydrate re-render from the backend, so replay fidelity is unaffected.

**References.** A reference goes the other way: into a backend that has told us
what it accepts. `normalize_reference` is where that contract is *enforced*, not
merely aimed at -- see its docstring for the two-rule split between a destination
that declared one and a destination that did not.
"""

from __future__ import annotations

import io

from PIL import Image

from .contracts import ImageGenerationError

_WEBP_QUALITY = 95
# Ceiling on a *phone-sized* reference upload, not a target size: identity-edit
# workflows are the ones that lose face detail to a downscale, and a graph wanting
# a specific size already carries a scale node (ImageScaleToTotalPixels).
_REFERENCE_MAX_EDGE = 4096
_REFERENCE_MAX_BYTES = 8 * 1024 * 1024

_FORMATS = {"image/webp": "WEBP", "image/png": "PNG", "image/jpeg": "JPEG"}
# Which mime to convert *to* when a destination declares a list. Ordered by bytes
# per unit of visible quality, not by the order the provider happened to list
# them: a reference is photographic content, PNG is lossless, and picking it turns
# the 0.37 MB WebP a render is already stored as into a 1.05 MB body. Lossy first.
_TARGET_PREFERENCE = ("image/webp", "image/jpeg", "image/png")
# Tried in order until one fits `max_bytes`. Quality moves before resolution
# because face identity -- the thing a reference exists to carry -- survives q75
# far better than it survives a halved edge. The first rung is the display
# quality, so a reference that already fits re-encodes to exactly what it would
# have before this ladder existed.
_REFERENCE_QUALITIES = (95, 85, 75)
_REFERENCE_EDGES = (_REFERENCE_MAX_EDGE, 3072, 2048, 1536, 1024)


def _target_mime(allowed: tuple[str, ...]) -> str:
    """The mime to convert to for a destination that declared `allowed`."""
    if not allowed:
        return "image/webp"
    for candidate in _TARGET_PREFERENCE:
        if candidate in allowed:
            return candidate
    return allowed[0]


def _load(data: bytes, fmt: str) -> Image.Image:
    """A decoded copy in a mode the target format can save.

    Detached from the file handle (`.copy()` after `load()`) so the caller can
    re-encode it repeatedly down the ladder without reopening the source.
    """
    with Image.open(io.BytesIO(data)) as src:
        src.load()
        # JPEG has no alpha channel, so RGBA must be flattened rather than handed
        # to a save that would raise.
        wanted = ("RGB",) if fmt == "JPEG" else ("RGB", "RGBA")
        return src.copy() if src.mode in wanted else src.convert("RGB")


def _encode(image: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt == "WEBP":
        image.save(buf, format=fmt, quality=quality, method=4)
    elif fmt == "JPEG":
        image.save(buf, format=fmt, quality=quality, optimize=True)
    else:
        # PNG is lossless: quality is not a lever on it, only the edge is.
        image.save(buf, format=fmt, optimize=True)
    return buf.getvalue()


def _bounded(image: Image.Image, fmt: str, max_bytes: int) -> bytes:
    """The first encoding that fits `max_bytes`, else the smallest one tried.

    Returning the smallest attempt rather than raising keeps the *decision* about
    an unmeetable budget with the caller, which is the only place that knows
    whether the destination declared it as a contract or as a preference.
    """
    qualities = _REFERENCE_QUALITIES if fmt in ("WEBP", "JPEG") else (_WEBP_QUALITY,)
    longest = max(image.size)
    smallest = b""
    tried: set[int] = set()
    for edge in _REFERENCE_EDGES:
        # Clamped, never upscaled -- and a clamp that lands on a size already
        # encoded is skipped rather than paying for the same bytes twice.
        effective = min(edge, longest)
        if effective in tried:
            continue
        tried.add(effective)
        candidate_image = image
        if effective < longest:
            candidate_image = image.copy()
            candidate_image.thumbnail((effective, effective), Image.Resampling.LANCZOS)
        for quality in qualities:
            candidate = _encode(candidate_image, fmt, quality)
            if len(candidate) <= max_bytes:
                return candidate
            if not smallest or len(candidate) < len(smallest):
                smallest = candidate
    return smallest


def shrink_for_display(data: bytes, mime: str) -> tuple[bytes, str]:
    """Re-encode a render to WebP at full resolution, for storage and inlining.

    Never raises, and never grows a payload: this is purely an optimization, so
    an already-small source that encodes larger as WebP keeps its own bytes and
    an unreadable one is handed back untouched.
    """
    try:
        image = _load(data, "WEBP")
        out = _encode(image, "WEBP", _WEBP_QUALITY)
    except Exception:
        return data, mime
    return (out, "image/webp") if len(out) < len(data) else (data, mime)


def normalize_reference(
    data: bytes,
    mime: str,
    *,
    allowed: tuple[str, ...] = (),
    max_bytes: int = _REFERENCE_MAX_BYTES,
) -> tuple[bytes, str]:
    """Bound a reference image to what the backend about to receive it accepts.

    Untouched unless it genuinely breaches the contract -- a 12 MP camera upload,
    not a render.

    Two rules, split on whether the destination declared anything:

    * **`allowed` given** -- the destination stated a contract, so this enforces
      it and raises `ImageGenerationError` when it cannot be met. Shipping a WebP
      inside a JSON body that tells the provider PNG, or a 15 MB base64 payload
      under a declared 4 MB cap, is not a smaller failure than saying so.
    * **`allowed` empty** -- nothing was declared, so this stays best effort and
      never raises: a reference Orb's own decoder cannot read is still one the
      backend probably can.

    The size gate alone is not enough for the first rule and never was.
    `shrink_for_display` stores every render as WebP, so a reference resolving to
    the previous image is WebP and comfortably under both ceilings -- it would
    sail through unconverted. So a disallowed *input* mime forces the re-encode
    irrespective of size, and the byte budget is then satisfied by an actual
    ladder rather than by a single hopeful attempt.
    """
    target = _target_mime(allowed)
    fmt = _FORMATS.get(target, "WEBP")
    incompatible = bool(allowed) and mime not in allowed
    if not incompatible and len(data) <= max_bytes:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if max(probe.size) <= _REFERENCE_MAX_EDGE:
                    return data, mime
        except Exception:
            if not allowed:
                return data, mime
            raise ImageGenerationError(_unreadable(max_bytes)) from None
    try:
        image = _load(data, fmt)
    except Exception:
        if not allowed:
            return data, mime
        raise ImageGenerationError(_unreadable(max_bytes)) from None
    out = _bounded(image, fmt, max_bytes)
    if not out:
        if not allowed:
            return data, mime
        raise ImageGenerationError(_unreadable(max_bytes)) from None
    if len(out) > max_bytes and allowed:
        raise ImageGenerationError(
            f"This reference image is still {len(out) // (1024 * 1024)} MB after Orb resized it, and the image "
            f"backend accepts at most {max_bytes // (1024 * 1024)} MB. Use a smaller image."
        )
    return out, target


def _unreadable(max_bytes: int) -> str:
    return (
        "Orb could not read this reference image, so it cannot convert it to a format the image backend accepts. "
        f"Use a PNG, JPEG or WebP under {max_bytes // (1024 * 1024)} MB."
    )
