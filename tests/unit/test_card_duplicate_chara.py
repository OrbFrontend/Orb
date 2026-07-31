"""A PNG with two `chara` chunks must parse as the first one (SillyTavern/chub behavior)."""

import base64
import io
import json
import zlib

from PIL import Image, PngImagePlugin

from backend.features.cards.parsing import card_to_dict, parse


def _chara(alt_greetings):
    payload = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {"name": "Amy", "first_mes": "hi", "alternate_greetings": alt_greetings},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode("ascii")


def test_first_chara_chunk_wins(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("chara", _chara(["alt one"]))  # real card
    info.add_text("chara", _chara([]))  # stale copy appended later

    path = tmp_path / "dupe.png"
    Image.new("RGBA", (8, 8)).save(path, format="PNG", pnginfo=info)

    # Sanity: PIL itself collapses the duplicate and keeps the stale one.
    with Image.open(path) as img:
        assert json.loads(base64.b64decode(img.info["chara"]))["data"]["alternate_greetings"] == []

    assert card_to_dict(parse(str(path)))["alternate_greetings"] == ["alt one"]


def test_single_chara_chunk_unaffected(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("chara", _chara(["only"]))
    path = tmp_path / "single.png"
    Image.new("RGBA", (8, 8)).save(path, format="PNG", pnginfo=info)

    assert card_to_dict(parse(str(path)))["alternate_greetings"] == ["only"]


def test_zero_length_chunk_does_not_hang(tmp_path):
    """Guards the manual chunk walk against a stall on an empty chunk."""
    info = PngImagePlugin.PngInfo()
    info.add_text("chara", _chara(["ok"]))
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8)).save(buf, format="PNG", pnginfo=info)
    raw = buf.getvalue()
    empty = (0).to_bytes(4, "big") + b"prVt" + zlib.crc32(b"prVt").to_bytes(4, "big")  # length-0 private chunk
    cut = 8 + 12 + 13  # after the signature + IHDR, which must stay first
    path = tmp_path / "empty_chunk.png"
    path.write_bytes(raw[:cut] + empty + raw[cut:])

    assert card_to_dict(parse(str(path)))["alternate_greetings"] == ["ok"]
