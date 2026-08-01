import io

from PIL import Image

from backend.workflows.image_gen.engine.display_encode import (
    normalize_reference,
    shrink_for_display,
)


def _png(w, h):
    buf = io.BytesIO()
    # Noise, not a flat fill: a flat image compresses to almost nothing and would
    # hide whether the WebP re-encode actually shrinks a real render.
    img = Image.effect_noise((w, h), 64).convert("RGB")
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_reencodes_to_webp_at_full_resolution():
    src = _png(1472, 2304)
    out, mime = shrink_for_display(src, "image/png")
    assert mime == "image/webp"
    assert len(out) < len(src)
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1472, 2304)  # resolution preserved


def test_non_image_bytes_pass_through_untouched():
    junk = b"not an image"
    assert shrink_for_display(junk, "image/png") == (junk, "image/png")


def test_a_normal_reference_is_uploaded_byte_for_byte():
    """Identity-edit workflows are exactly the ones that lose face detail to a
    downscale, so the cap only exists to stop a 12 MP phone upload -- a render or
    an avatar must reach ComfyUI untouched."""
    src = _png(1024, 1536)
    assert normalize_reference(src, "image/png") == (src, "image/png")


def test_an_oversized_reference_is_bounded():
    src = _png(5000, 3000)
    out, mime = normalize_reference(src, "image/png")
    assert mime == "image/webp"
    with Image.open(io.BytesIO(out)) as img:
        assert max(img.size) == 4096
        assert abs(img.size[0] / img.size[1] - 5000 / 3000) < 0.01  # aspect preserved


def test_a_reference_that_cannot_be_re_encoded_is_sent_as_is():
    """A reference Orb cannot read is still one ComfyUI probably can, so failure
    here degrades rather than sinking the generation."""
    junk = b"not an image"
    assert normalize_reference(junk, "image/png") == (junk, "image/png")
