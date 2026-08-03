"""Re-encoding on the way out (display) and on the way in (references).

The reference half is the one with a contract: a destination that declares
`allowed`/`max_bytes` must be honoured or refused, while one that declares nothing
stays best effort. Both halves live here rather than beside each backend, because
the rule is the same wherever the bytes are going.
"""

import io

import pytest
from PIL import Image

from backend.workflows.image_gen.engine.contracts import ImageGenerationError
from backend.workflows.image_gen.engine.display_encode import (
    normalize_reference,
    shrink_for_display,
)

_CAP = 4 * 1024 * 1024


def _png(w, h, fmt: str = "PNG") -> bytes:
    # Noise, not a flat fill: a flat image compresses to almost nothing and would
    # hide whether the re-encode actually shrinks a real render.
    buf = io.BytesIO()
    Image.effect_noise((w, h), 64).convert("RGB").save(buf, format=fmt)
    return buf.getvalue()


# ── display ──────────────────────────────────────────────────────────────────


def test_reencodes_to_webp_at_full_resolution():
    src = _png(1472, 2304)
    out, mime = shrink_for_display(src, "image/png")
    assert mime == "image/webp"
    assert len(out) < len(src)
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1472, 2304)  # resolution preserved


def test_non_image_bytes_pass_through_untouched():
    assert shrink_for_display(b"not an image", "image/png") == (b"not an image", "image/png")


# ── references, with nothing declared ────────────────────────────────────────


def test_a_normal_reference_is_uploaded_byte_for_byte():
    """Identity-edit workflows are the ones that lose face detail to a downscale,
    so the cap only exists to stop a 12 MP phone upload."""
    src = _png(1024, 1536)
    assert normalize_reference(src, "image/png") == (src, "image/png")
    # With no contract to break, a reference Orb cannot read is still one ComfyUI
    # probably can, so it is handed back rather than refused.
    assert normalize_reference(b"<svg/>", "image/svg+xml") == (b"<svg/>", "image/svg+xml")


def test_an_oversized_reference_is_bounded():
    out, mime = normalize_reference(_png(5000, 3000), "image/png")
    assert mime == "image/webp"
    with Image.open(io.BytesIO(out)) as img:
        assert max(img.size) == 4096
        assert abs(img.size[0] / img.size[1] - 5000 / 3000) < 0.01  # aspect preserved


# ── references, against a declared contract ──────────────────────────────────


def test_a_stored_webp_reference_arrives_as_a_mime_the_provider_accepts():
    """The common path, not a corner: every render is stored as WebP, so a reference
    resolving to `previous` is WebP and comfortably under both size ceilings. A
    size-only gate would let it sail into a JSON body that told the provider PNG."""
    data, mime = normalize_reference(_png(512, 512, "WEBP"), "image/webp", allowed=("image/png", "image/jpeg"), max_bytes=_CAP)

    assert mime in ("image/png", "image/jpeg")
    # And the bytes really are that format, not just relabelled.
    with Image.open(io.BytesIO(data)) as probe:
        assert probe.format == "JPEG"


def test_the_conversion_target_is_the_lossy_one_the_provider_allows():
    """PNG is lossless and a reference is photographic, so preferring it tripled the
    bytes of the WebP every render is already stored as -- on the common path, not an
    edge case. The preference is by compression, not by preset order."""
    stored, stored_mime = shrink_for_display(_png(1024, 1536), "image/png")
    as_png, _ = normalize_reference(stored, stored_mime, allowed=("image/png",), max_bytes=_CAP)
    as_either, mime = normalize_reference(stored, stored_mime, allowed=("image/png", "image/jpeg"), max_bytes=_CAP)

    assert mime == "image/jpeg"
    assert len(as_either) < len(as_png)


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        # Over the cap in a mime the provider already accepts: a "keep whichever is
        # smaller" rule hands this straight back, still oversized.
        (_png(4000, 3000, "JPEG"), "image/jpeg"),
        # And the same picture arriving as the WebP a render is stored as.
        (_png(4000, 3000, "WEBP"), "image/webp"),
    ],
    ids=["already an accepted mime", "needs conversion too"],
)
def test_an_oversized_reference_is_brought_under_a_declared_cap(data, mime):
    """`max_bytes` is a contract, not a hint: the reference rides base64 inside a
    JSON body, so an unenforced cap is a multi-megabyte POST the provider rejects."""
    cap = 1024 * 1024
    assert len(data) > cap
    out, out_mime = normalize_reference(data, mime, allowed=("image/png", "image/jpeg"), max_bytes=cap)

    assert len(out) <= cap
    assert out_mime in ("image/png", "image/jpeg")


def test_a_reference_orb_cannot_decode_is_refused_rather_than_mislabelled():
    """A declared `allowed` list means the destination cannot read the input format.
    Returning the input unchanged is what sent an SVG or a HEIC upload to a provider
    inside a body that declared PNG."""
    with pytest.raises(ImageGenerationError, match="could not read"):
        normalize_reference(b"<svg/>", "image/svg+xml", allowed=("image/png", "image/jpeg"), max_bytes=_CAP)
