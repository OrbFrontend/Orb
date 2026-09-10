"""The duplicate finder's signals: normalization, hashing, and avatar hashing.

Pure functions, no app stack. These decide what "the same card" means, so each
test states the rule it protects and why the rule exists — a threshold that
drifts here silently changes what the scan reports.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend.features.library_dedupe import (
    AVATAR_MAX_DISTANCE,
    MAX_AVATAR_PIXELS,
    SKETCH_SIZE,
    body_hash,
    dhash_from_image_bytes,
    field_hashes,
    hamming,
    normalize_field,
    shingle_sketch,
    shingles,
)


def _card(**overrides) -> dict:
    card = {
        "id": "card-1",
        "name": "Lira",
        "description": "A hedge-witch who keeps bees at the edge of the wood.",
        "personality": "Dry, patient, unwilling to explain herself twice.",
        "scenario": "",
        "first_mes": "*She does not look up from the hive.* You are late.",
        "mes_example": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "creator": "someone",
        "tags": [],
    }
    card.update(overrides)
    return card


# ── Normalization ────────────────────────────────────────────────────────────


def test_normalization_folds_case_and_collapses_whitespace():
    # The same prose re-wrapped by an editor, or re-cased by a site, is the same
    # prose — a duplicate that survives a reflow is exactly the case this feature
    # exists for.
    assert normalize_field("  A  Hedge-Witch\n\tkeeps   bees. ") == "a hedge-witch keeps bees."


def test_alternate_greetings_are_sorted_and_deduplicated():
    # Order is presentation, and the same greeting listed twice is not a
    # different card, so neither may change the signature.
    assert normalize_field(["Two", "One", "one"]) == normalize_field(["ONE", "Two"])


def test_a_non_string_field_normalizes_to_nothing_rather_than_raising():
    # Cards come from arbitrary imported PNGs; a field holding a number or None
    # must not fail a library-wide scan.
    assert normalize_field(None) == "" and normalize_field(7) == ""


# ── The body hash ────────────────────────────────────────────────────────────


def test_retagging_a_card_does_not_change_its_body_hash():
    # Tags are organizational. If they fed the signature, one auto-tagging run
    # would lapse every dismissal in the library.
    assert body_hash(_card(tags=["Fantasy"])) == body_hash(_card(tags=["Romance", "Slow burn"]))


def test_a_generated_public_profile_does_not_change_its_body_hash():
    # Same rule, and the brief names it: a generated public profile must not stop
    # two otherwise identical cards from being detected.
    profile = {"orb": {"public_profile": {"appearance": "Tall.", "role": "Beekeeper."}}}
    assert body_hash(_card(extensions=profile)) == body_hash(_card())


def test_moving_text_between_two_fields_changes_the_body_hash():
    # Field names are folded into the payload, so a card whose personality was
    # pasted into its description is a different card, not a byte-identical one.
    moved = _card(description="A hedge-witch. Dry, patient.", personality="")
    assert body_hash(moved) != body_hash(_card(description="A hedge-witch.", personality="Dry, patient."))


def test_a_card_with_no_content_at_all_has_no_body_hash():
    # "" is the skip signal everywhere downstream. Two blank cards must not
    # block together on the emptiness they share.
    blank = {key: "" for key in _card()}
    assert body_hash(blank) == ""


# ── Field hashes ─────────────────────────────────────────────────────────────


def test_empty_fields_are_omitted_from_the_field_hashes():
    # Most cards ship a blank system_prompt, scenario and mes_example. Hashing ""
    # would drop the whole library into one block and produce a flood of
    # "same system prompt" reasons for cards that each merely left it empty.
    hashes = field_hashes(_card())
    assert "system_prompt" not in hashes and "scenario" not in hashes
    assert {"name", "description", "personality", "first_mes"} <= set(hashes)


def test_two_cards_agreeing_on_a_field_share_its_hash():
    assert field_hashes(_card(name="Lira"))["name"] == field_hashes(_card(name="  LIRA  "))["name"]


# ── Shingles ─────────────────────────────────────────────────────────────────


def test_shingle_hashes_are_stable_across_processes():
    # blake2b, not the built-in hash(): Python randomizes string and tuple
    # hashing per process, so sketches built with hash() would differ between
    # a scan and the next scan after a restart.
    import subprocess  # nosec B404
    import sys

    code = (
        "from backend.features.library_dedupe import shingles;"
        "print(sorted(shingles({'description': 'the quick brown fox jumps over the lazy dog'}))[0])"
    )
    out = subprocess.run(  # nosec B603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert int(out) == sorted(shingles({"description": "the quick brown fox jumps over the lazy dog"}))[0]


def test_the_sketch_is_the_smallest_hashes_and_is_capped():
    long_text = " ".join(f"word{i}" for i in range(400))
    values = shingles({"description": long_text})
    sketch = shingle_sketch(values)
    assert len(sketch) == SKETCH_SIZE
    assert list(sketch) == sorted(values)[:SKETCH_SIZE]


def test_a_card_shorter_than_the_shingle_width_produces_no_shingles():
    # Four tokens cannot form a 5-gram. It must yield an empty set rather than
    # raise, because a one-line card is a real card.
    assert shingles({"description": "she keeps the bees"}) == frozenset()


# ── Avatar hashing ───────────────────────────────────────────────────────────


def _art(width: int = 512, height: int = 768) -> Image.Image:
    """Deliberately high-frequency synthetic card art — the hard case for dHash."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7 + y * 3) % 256, (x * x + y) % 256, (x ^ y) % 256)
    return image


def _encode(image: Image.Image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def art() -> Image.Image:
    return _art()


def test_the_same_art_re_encoded_as_jpeg_keeps_its_dhash(art):
    # The four-sites-one-character case: each site re-encodes, so a byte hash of
    # avatar_b64 sees four different cards and only decoded pixels see one.
    original = dhash_from_image_bytes(_encode(art, "PNG"))
    assert hamming(original, dhash_from_image_bytes(_encode(art, "JPEG", quality=82))) <= AVATAR_MAX_DISTANCE
    assert hamming(original, dhash_from_image_bytes(_encode(art, "WEBP", quality=80))) <= AVATAR_MAX_DISTANCE


def test_the_same_art_downscaled_by_half_stays_within_the_avatar_threshold(art):
    # This is why the threshold is 8 and not the textbook 4: on art this
    # high-frequency a plain 2x resize — what a different download site does —
    # already costs several bits on its own.
    original = dhash_from_image_bytes(_encode(art, "PNG"))
    half = art.resize((art.width // 2, art.height // 2), Image.Resampling.LANCZOS)
    third = art.resize((art.width // 3, art.height // 3), Image.Resampling.LANCZOS)
    assert hamming(original, dhash_from_image_bytes(_encode(half, "PNG"))) <= AVATAR_MAX_DISTANCE
    assert hamming(original, dhash_from_image_bytes(_encode(third, "PNG"))) <= AVATAR_MAX_DISTANCE


def test_different_art_is_far_outside_the_avatar_threshold(art):
    other = Image.new("RGB", (512, 768))
    pixels = other.load()
    for y in range(768):
        for x in range(512):
            pixels[x, y] = ((x // 8) % 256, (y // 4) % 256, 128)
    distance = hamming(dhash_from_image_bytes(_encode(art, "PNG")), dhash_from_image_bytes(_encode(other, "PNG")))
    assert distance > AVATAR_MAX_DISTANCE


def test_an_undecodable_avatar_yields_no_hash_rather_than_raising():
    # One corrupt avatar in a 2000-card library must not fail the scan, and ""
    # is the value that excludes it from avatar blocking entirely.
    assert dhash_from_image_bytes(b"") == ""
    assert dhash_from_image_bytes(b"not an image at all") == ""
    assert dhash_from_image_bytes(_encode(_art(8, 8), "PNG")[:40]) == ""


def test_an_oversized_image_is_refused_from_its_header_alone(monkeypatch):
    """The decompression-bomb guard is local, not PIL's global MAX_IMAGE_PIXELS.

    Raising or lowering that global from here would silently change behaviour
    for card parsing and the image-gen workflow, so the size check reads
    ``Image.open``'s lazily-parsed header and bails before ``convert("L")``
    decodes a single row.
    """
    import backend.features.library_dedupe.fingerprint as fingerprint

    monkeypatch.setattr(fingerprint, "MAX_AVATAR_PIXELS", 16)
    assert fingerprint.dhash_from_image_bytes(_encode(_art(64, 64), "PNG")) == ""
    # ...and the real ceiling is nowhere near ordinary card art.
    assert MAX_AVATAR_PIXELS > 1024 * 1536


def test_a_dhash_is_sixteen_hex_characters(art):
    digest = dhash_from_image_bytes(_encode(art, "PNG"))
    assert len(digest) == 16 and int(digest, 16) >= 0
