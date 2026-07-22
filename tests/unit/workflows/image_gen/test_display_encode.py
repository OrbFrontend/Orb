import io

from PIL import Image

from backend.workflows.image_gen.engine.display_encode import shrink_for_display


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
