"""Re-encode images on the way out (display) and on the way in (references).

**Display.** A full-resolution PNG render is ~4.7 MB, so a chat holding eight of
them inlines ~33 MB of base64 the browser parses on every open. WebP at q95 is
visually identical and roughly 6x smaller. Resolution is preserved, so what shows
inline is still the full-quality picture; the conversion is lossy and one-way, but
reroll/rehydrate re-render from the backend, so replay fidelity is unaffected.

**References.** A reference goes the other way, into a backend that has declared
what it accepts. `normalize_reference` is where that contract is *enforced* -- see
its docstring for the split between a destination that declared one and one that
did not.
"""

from __future__ import annotations

import io

from PIL import Image

from .contracts import ImageGenerationError

_WEBP_QUALITY = 95
# A ceiling on phone-sized uploads, not a target size: identity-edit workflows lose
# face detail to a downscale, and a graph wanting a specific size already carries a
# scale node.
_REFERENCE_MAX_EDGE = 4096
_REFERENCE_MAX_BYTES = 8 * 1024 * 1024

_FORMATS = {"image/webp": "WEBP", "image/png": "PNG", "image/jpeg": "JPEG"}
# Which mime to convert *to* when a destination declares a list, ordered by bytes
# per unit of visible quality rather than by the provider's listing order: a
# reference is photographic, and preferring lossless PNG turns the 0.37 MB WebP a
# render is stored as into a 1.05 MB body.
_TARGET_PREFERENCE = ("image/webp", "image/jpeg", "image/png")
# Tried in order until one fits `max_bytes`. Quality moves before resolution because
# face identity -- what a reference exists to carry -- survives q75 far better than
# a halved edge. The first rung is the display quality, so a reference that already
# fits re-encodes to exactly what it would have anyway.
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
    """A decoded copy in a mode the target format can save, detached from the file
    handle so the caller can re-encode it down the ladder without reopening."""
    with Image.open(io.BytesIO(data)) as src:
        src.load()
        # JPEG has no alpha channel, so RGBA is flattened rather than handed to a
        # save that would raise.
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

    Returning the smallest attempt rather than raising leaves the decision about an
    unmeetable budget with the caller, the only place that knows whether the
    destination declared it as a contract or as a preference.
    """
    qualities = _REFERENCE_QUALITIES if fmt in ("WEBP", "JPEG") else (_WEBP_QUALITY,)
    longest = max(image.size)
    smallest = b""
    tried: set[int] = set()
    for edge in _REFERENCE_EDGES:
        # Clamped, never upscaled, and a clamp landing on an already-encoded size is
        # skipped rather than paying for the same bytes twice.
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
    not a render. Two rules, split on whether the destination declared anything:

    * **`allowed` given** -- a stated contract, enforced, raising
      `ImageGenerationError` when it cannot be met. Shipping a WebP inside a JSON
      body that tells the provider PNG is not a smaller failure than saying so.
    * **`allowed` empty** -- nothing declared, so best effort and never raising: a
      reference Orb cannot decode is still one the backend probably can.

    A size gate alone would not do for the first rule: every render is stored as
    WebP, so a reference resolving to the previous image sails under both ceilings
    unconverted. A disallowed *input* mime therefore forces the re-encode
    irrespective of size, and the byte budget is met by the ladder above.
    """
    target = _target_mime(allowed)
    fmt = _FORMATS.get(target, "WEBP")

    def give_up() -> tuple[bytes, str]:
        """Orb cannot convert these bytes: hand them back, or refuse a contract."""
        if not allowed:
            return data, mime
        raise ImageGenerationError(_unreadable(max_bytes)) from None

    incompatible = bool(allowed) and mime not in allowed
    if not incompatible and len(data) <= max_bytes:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if max(probe.size) <= _REFERENCE_MAX_EDGE:
                    return data, mime
        except Exception:
            return give_up()
    try:
        image = _load(data, fmt)
    except Exception:
        return give_up()
    out = _bounded(image, fmt, max_bytes)
    if not out:
        return give_up()
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
